#!/usr/bin/env python3
"""Decide what Stop and SubagentStop do with each pending bead-label marker.

A gate-mode skill leaves a marker in <git-common-dir>/pending-bead-labels when
it starts. The label script's Stop handler runs this module to decide every
marker in one pass, then acts on each decision: it applies the label, logs the
outcome, and removes the marker. SubagentStop runs it to find the accepted
markers whose acceptance-verifier agent finished and wrote no report.

    python3 resolve_stop_markers.py stop <marker-dir> <git-dir> <qa-reports-dir> <now>
        <head> <repo-root>
    python3 resolve_stop_markers.py subagent-stop <marker-dir> <git-dir> <session>

It prints one record per marker that needs an action, in marker order. A record
is eleven fields, each ended by a NUL byte, since a marker can hold any text:
action, marker path, label, bead, skill, remove (1 or 0), message, outcome, and
the kind, run ID, and owner of the invocation claim to finish once the marker
is gone. The last three are empty unless the record consumed its run's report.

    label     apply the label, then log the outcome; a failed write logs FAILED
    confirm   an inject marker: read the bead and say whether the label landed
    log       log the outcome and do nothing else

A marker that is waiting for its report prints nothing. A marker this module
cannot read is decided on its own, so one malformed marker never stops the
markers after it from resolving.

A marker for a validation run, /quality-gates or /verify-acceptance, can carry
three more lines: the invocation kind, its run ID, and the git directory of the
worktree that started it. Such a marker reads only its own worktree's reports,
and only a report of its own run: the run's copy under validation-runs/, or a
latest report whose run_id matches. A latest report that names another run_id
is never consumed, even at an unchanged HEAD. A version 2 report names no run,
so the newer-than-the-marker rule still decides it during preparation. A marker
without those lines is a legacy marker and resolves as before.

A quality-gates or acceptance report that carries any version 3 field goes
through its format's validator (docs/validation-report-contract.md), never the
legacy parsers. It earns the label only as a valid success that is current for
the marker: its run, its bead, the descriptor's expected inputs, HEAD, a clean
tree, and every cited file's digest. The minimum version is the higher of
MINIMUM_VERSION and the one the run's descriptor mode selects, so a strict run
refuses a version 2 report. A latest report of another run, written after the
marker, is logged, and the marker stays pending for its own run's report.

Every fact arrives as an argument (ADR 0011): it runs no git and no bd, and the
tracker writes stay in the label script. Each report's rules come from
bead_label_reports.py beside it. Stdlib only, and Python 3.9, since
/usr/bin/python3 is what a consumer repository is guaranteed to have.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import bead_label_reports as reports

EXIT_OK = 0
EXIT_USAGE = 2

# A pending marker older than this is abandoned rather than resolved, so a run
# that never finished cannot label a later unrelated turn.
MARKER_TTL_SECONDS = 21600  # 6 hours

LABEL = "label"
CONFIRM = "confirm"
LOG = "log"

# The marker's third line names the skill, and the skill picks the report
# format. Anything unrecognized is a /qa marker, which is what every marker
# meant before that line was used.
FORMAT_OF_SKILL = {
    "feature-development": "build",
    "tadw:feature-development": "build",
    "quality-gates": "quality-gates",
    "tadw:quality-gates": "quality-gates",
    "verify-acceptance": "acceptance",
    "tadw:verify-acceptance": "acceptance",
}
QA_FORMAT = "qa"

# One JSON report per clone or worktree, read from <git-dir> and never the
# common dir, so a worktree reads its own run and not a sibling's.
REPORT_FILE_OF_FORMAT = {
    "build": "build-report.json",
    "quality-gates": "quality-gates-report.json",
    "acceptance": "acceptance-report.json",
}
QA_REPORT = re.compile(r"qa-report-.*\.md")

# Where a validation writer keeps each run's copy of its report.
RUNS_DIRECTORY = "validation-runs"
RUN_REPORT = "report.json"

# The formats whose report records the bead it was written for. Every other
# report is taken to describe the marker's own bead.
BEAD_NAMING_FORMATS = ("build", "acceptance")

# Each validated format's validator module and the skill that owns it. An
# installer copies both beside this module; the plugin keeps them under skills/.
VALIDATOR_OF_FORMAT = {
    "quality-gates": ("report_contract", "quality-gates"),
    "acceptance": ("acceptance_contract", "verify-acceptance"),
}
DESCRIPTOR = "invocation.json"

# The fields only a version 3 report has. A report with any of them is
# validated, so a version 3 report cannot reach a legacy count parser.
VERSION_3_FIELDS = ("run_id", "kind", "producer", "expected", "evidence")

# The lowest report version Stop accepts as success. The preparation release
# reads version 2; the strict release (tadw-31au) raises it. A descriptor can
# raise the minimum through its mode, and nothing can lower it.
MINIMUM_VERSION = 2
STRICT_MINIMUM = 3
MINIMUM_OF_MODE = {"preparation": MINIMUM_VERSION, "strict": STRICT_MINIMUM}

VALID_SUCCESS = "valid-success"
NOT_USABLE = "not-usable"

# The owner the label script reserves a run under, which `prepare --pending`
# adopts. Kept in step with reserve_invocation in the label script.
HOOK_OWNER = "bead-label-hook"

DIGITS = re.compile(r"[0-9]+")

ACCEPTED_PREFIX = "accepted__"
ACCEPTANCE_AGENT = "acceptance-verifier"
# The plugin version whose acceptance-verifier first wrote a report file.
ACCEPTANCE_REPORT_SINCE = "4.3.1"


@dataclass(frozen=True)
class Places:
    """Where the reports live, and the time to judge each marker's age against."""

    git_dir: Path
    qa_dir: Path
    now: int
    head: str = ""
    repo_root: Path | None = None


@dataclass(frozen=True)
class Marker:
    """One pending marker: the filename carries the label, the lines the rest."""

    path: Path
    lines: tuple[str, ...]

    @classmethod
    def read(cls, path: Path) -> Marker:
        return cls(path, tuple(read_marker_text(path).split("\n")))

    @property
    def label(self) -> str:
        """The text before the first "__", or nothing when there is no prefix."""
        label, separator, _ = self.path.name.partition("__")
        return label if separator else ""

    def line(self, number: int) -> str:
        return self.lines[number - 1] if number <= len(self.lines) else ""

    @property
    def created(self) -> str:
        return self.line(1)

    @property
    def bead(self) -> str:
        return self.line(2)

    @property
    def skill(self) -> str:
        return self.line(3)

    @property
    def mode(self) -> str:
        """A marker written before the mode line existed is a gate marker."""
        return self.line(4) or "gate"

    @property
    def session(self) -> str:
        return self.line(5)

    @property
    def kind(self) -> str:
        return self.line(6)

    @property
    def run_id(self) -> str:
        """The validation invocation this marker waits for, or nothing for a legacy marker."""
        return self.line(7)

    @property
    def git_dir(self) -> str:
        return self.line(8)

    @property
    def report_format(self) -> str:
        return FORMAT_OF_SKILL.get(self.skill, QA_FORMAT)

    @property
    def owner(self) -> str:
        """The owner the label script reserved this marker's run under."""
        return f"{HOOK_OWNER} session {self.session}" if self.session else HOOK_OWNER

    def belongs_to(self, git_dir: Path) -> bool:
        """Markers are shared by every worktree, so each records the git dir it reads."""
        return not self.git_dir or Path(self.git_dir) == git_dir


@dataclass(frozen=True)
class Finish:
    """The invocation claim the label script finishes once the marker is gone."""

    kind: str
    run_id: str
    owner: str


NO_FINISH = Finish("", "", "")


@dataclass(frozen=True)
class Decision:
    action: str
    marker: Marker
    remove: bool
    message: str = ""
    outcome: str = ""
    finish: Finish | None = None

    @property
    def fields(self) -> tuple[str, ...]:
        marker = self.marker
        flag = "1" if self.remove else "0"
        path = str(marker.path)
        claim = self.finish or NO_FINISH
        return (
            self.action,
            path,
            marker.label,
            marker.bead,
            marker.skill,
            flag,
            self.message,
            self.outcome,
            claim.kind,
            claim.run_id,
            claim.owner,
        )


# ---------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------


def decide_stop(marker_dir: Path, places: Places) -> Iterator[Decision]:
    """One decision per marker that needs an action, in marker order."""
    for path in marker_paths(marker_dir):
        decision = decide_isolated(path, places)
        if decision is not None:
            yield decision


def decide_isolated(path: Path, places: Places) -> Decision | None:
    """Decides one marker, so that a failure here leaves the next marker alone."""
    marker = Marker.read(path)
    try:
        return decide_marker(marker, places)
    except Exception as error:  # noqa: BLE001, one marker must never stop the rest
        return Decision(
            LOG,
            marker,
            remove=False,
            message=f"could not resolve marker {path.name} ({error!r}); leaving it",
            outcome=f"FAILED to resolve {path.name}, {type(error).__name__}",
        )


def decide_marker(marker: Marker, places: Places) -> Decision | None:
    """The first rule that decides the marker, or None while its run is still going."""
    for rule in (refuse_unlabeled, refuse_unreadable, abandon_stale, confirm_inject):
        decision = rule(marker, places)
        if decision is not None:
            return decision
    return decide_gate(marker, places)


def refuse_unlabeled(marker: Marker, _places: Places) -> Decision | None:
    """Hardcoding one label would mislabel every other gate entry, so a marker with none goes."""
    if marker.label:
        return None
    name = marker.path.name
    return Decision(
        LOG,
        marker,
        remove=True,
        message=f"marker {name} carries no label prefix; discarding",
        outcome=f"discarded {name}, the filename carries no label prefix",
    )


def refuse_unreadable(marker: Marker, _places: Places) -> Decision | None:
    if DIGITS.fullmatch(marker.created) and marker.bead:
        return None
    field = "bead id" if DIGITS.fullmatch(marker.created) else "timestamp"
    return Decision(
        LOG,
        marker,
        remove=True,
        message=f"marker {marker.path.name} has an unreadable {field}; discarding",
        outcome=f"discarded {marker.label}, the marker header has an unreadable {field}",
    )


def abandon_stale(marker: Marker, places: Places) -> Decision | None:
    if places.now - int(marker.created) <= MARKER_TTL_SECONDS:
        return None
    return Decision(
        LOG,
        marker,
        remove=True,
        message=f"abandoning stale marker {marker.path.name}",
        outcome=f"abandoned {marker.label}, {abandon_reason(marker, places)}",
    )


def abandon_reason(marker: Marker, places: Places) -> str:
    """Why a marker past its TTL goes, in the words the log carries.

    An accepted gate marker has a second way out, since SubagentStop removes it
    when the agent finishes with no report. Reaching the TTL with no report
    means neither signal came. With a report present, the plain words stay true.
    """
    waiting = marker.mode == "gate" and marker.report_format == "acceptance"
    if waiting and report_after(marker, places) is None:
        return "no report arrived and no agent finish was seen"
    return "the marker outlived its TTL"


def confirm_inject(marker: Marker, _places: Places) -> Decision | None:
    """Inject mode has no artifact, so only the bead can say whether its label landed."""
    if marker.mode != "inject":
        return None
    return Decision(CONFIRM, marker, remove=True)


def decide_gate(marker: Marker, places: Places) -> Decision | None:
    """Only a report of this run, from this marker's worktree, can describe it."""
    if not marker.belongs_to(places.git_dir):
        return None
    report = report_after(marker, places)
    if report is None:
        return report_of_another_run(marker, places)
    return bead_elsewhere(marker, report) or judge(marker, report, places)


def of_own_run(marker: Marker, report: Path) -> bool:
    """A report naming the marker's run is that run's, so its validator judges the bead."""
    return bool(marker.run_id) and named_run(report) == marker.run_id


def bead_elsewhere(marker: Marker, report: Path) -> Decision | None:
    """A report that is not of the marker's own run must name the marker's bead."""
    if of_own_run(marker, report):
        return None
    named = report_bead(report, marker)
    return None if named == marker.bead else leave_for_own_report(marker, report, named)


def judge(marker: Marker, report: Path, places: Places) -> Decision:
    """A validated format goes to its validator unless its legacy parser still owns the report."""
    if marker.report_format not in VALIDATOR_OF_FORMAT:
        return judge_report(marker, report)
    run = RunFacts.read(marker, places)
    if needs_validator(report, run.minimum_version):
        return judge_validated(marker, report, places, run)
    return judge_report(marker, report)


def report_of_another_run(marker: Marker, places: Places) -> Decision | None:
    """Says why a run's marker still waits when another run's report took the latest file.

    A latest report written before the marker is an earlier run's, so the
    marker waits for its own in silence. One written after it is a competing
    publication, and the log says the marker did not consume it.
    """
    competing = competing_report(marker, places)
    if competing is None:
        return None
    latest, latest_run = competing
    names = f"run {latest_run}" if latest_run else "no valid run"
    return Decision(
        LOG,
        marker,
        remove=False,
        message=f"report {latest} names {names}, not run {marker.run_id}; leaving the marker",
        outcome=f"left {marker.label} pending, the latest report names {names}",
    )


def competing_report(marker: Marker, places: Places) -> tuple[Path, str] | None:
    """The latest report and the run it names, when another run wrote it after the marker."""
    name = REPORT_FILE_OF_FORMAT.get(marker.report_format)
    if not marker.run_id or name is None:
        return None
    latest = places.git_dir / name
    latest_run = named_run(latest)
    competing = latest_run not in (None, marker.run_id) and newer(latest, marker.path)
    return (latest, latest_run) if competing else None


def leave_for_own_report(marker: Marker, report: Path, named: str) -> Decision:
    """Markers are shared by every worktree and reports are not (tadw-34bo)."""
    names = named or "no bead"
    return Decision(
        LOG,
        marker,
        remove=False,
        message=f"report {report} names {names}, not {marker.bead}; leaving the marker pending",
        outcome=f"left {marker.label} pending, the report names {names}",
    )


def judge_report(marker: Marker, report: Path) -> Decision:
    verdict = reports.PARSERS[marker.report_format](report)
    if verdict.passed:
        outcome = f"applied {marker.label}, the report passed"
        return Decision(LABEL, marker, remove=True, message=verdict.reason, outcome=outcome)
    outcome = f"withheld {marker.label}, the report did not pass"
    return Decision(LOG, marker, remove=True, message=verdict.reason, outcome=outcome)


# ---------------------------------------------------------------------
# Validated reports
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class RunFacts:
    """What the marker's run descriptor fixed before grading: its inputs and its minimum."""

    expected: object
    minimum_version: int

    @classmethod
    def read(cls, marker: Marker, places: Places) -> RunFacts:
        """A legacy marker, or a run with no descriptor, has no expected inputs to match."""
        descriptor = None
        if marker.run_id:
            path = places.git_dir / RUNS_DIRECTORY / marker.run_id / DESCRIPTOR
            descriptor = reports.read_json_object(path)
        if descriptor is None:
            return cls(None, MINIMUM_VERSION)
        return cls(descriptor.get("expected"), minimum_of(descriptor.get("mode")))


def minimum_of(mode: object) -> int:
    """The minimum a descriptor's mode selects; a mode Stop does not know is strict."""
    known = isinstance(mode, str) and mode in MINIMUM_OF_MODE
    return max(MINIMUM_VERSION, MINIMUM_OF_MODE[mode] if known else STRICT_MINIMUM)


def needs_validator(report: Path, minimum_version: int) -> bool:
    """Only a legacy report under a legacy minimum keeps its legacy parser.

    Text that is not a JSON object goes to the validator too, which refuses it
    with the manifest's reason, as the legacy parser refused it with its own.
    """
    fields = reports.read_json_object(report)
    if fields is None or minimum_version > MINIMUM_VERSION:
        return True
    named_version_3 = fields.get("version") == STRICT_MINIMUM
    return named_version_3 or any(field in fields for field in VERSION_3_FIELDS)


def judge_validated(marker: Marker, report: Path, places: Places, run: RunFacts) -> Decision:
    """Labels only a valid success that is current for this marker's run and bead."""
    contract = validator(marker.report_format)
    facts = contract.CallerFacts(
        run_id=marker.run_id or None,
        head=places.head,
        bead=marker.bead,
        expected=run.expected,
        git_dir=places.git_dir,
        repo_root=places.repo_root,
    )
    verdict = contract.classify(reports.read_text(report), facts, run.minimum_version)
    return validated_decision(marker, report, verdict)


def validated_decision(marker: Marker, report: Path, verdict) -> Decision:  # noqa: ANN001
    """Every validated report consumes its marker; only a valid success labels."""
    finish = consumed_claim(marker, report)
    if verdict.classification == VALID_SUCCESS:
        return Decision(
            LABEL,
            marker,
            remove=True,
            message=f"report {report} is valid and current: {verdict.conclusion}",
            outcome=f"applied {marker.label}, the report is a current valid success",
            finish=finish,
        )
    why = withheld_because(verdict)
    message = f"{marker.report_format} report {report}: {why}; not labeling"
    outcome = f"withheld {marker.label}, {why}"
    return Decision(LOG, marker, remove=True, message=message, outcome=outcome, finish=finish)


def withheld_because(verdict) -> str:  # noqa: ANN001, the validator's own Decision type
    if verdict.classification != NOT_USABLE:
        return f"the report concluded {verdict.conclusion}"
    why = f"the report is {verdict.reason} ({verdict.rule}): {verdict.detail}"
    if verdict.reason == "below-minimum":
        why += "; a fresh version 3 run is needed"
    return why


def consumed_claim(marker: Marker, report: Path) -> Finish | None:
    """A report of the marker's own run ends its claim; any other report leaves it."""
    if not of_own_run(marker, report):
        return None
    return Finish(marker.kind or marker.report_format, marker.run_id, marker.owner)


def validator(report_format: str) -> ModuleType:
    """The format's validator, from beside this module or the plugin's skills/ layout.

    Loaded only when a report needs it, so a missing validator fails that one
    marker closed, through decide_isolated, and the legacy formats still work.
    """
    name, skill = VALIDATOR_OF_FORMAT[report_format]
    return sys.modules.get(name) or load_module(name, validator_path(name, skill))


def validator_path(name: str, skill: str) -> Path:
    here = Path(__file__).resolve().parent
    for path in (here / f"{name}.py", here.parent / "skills" / skill / "scripts" / f"{name}.py"):
        if path.is_file():
            return path
    raise ImportError(f"{name}.py is neither beside {here} nor under skills/{skill}/scripts")


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # A dataclass resolves its module through sys.modules while it is built.
    sys.modules[name] = module
    execute_registered(spec, name)
    return module


def execute_registered(spec, name: str) -> None:  # noqa: ANN001, an importlib ModuleSpec
    """Runs the module's code, and unregisters it when that fails.

    A half-built module left in sys.modules would serve the next marker's lookup.
    """
    try:
        spec.loader.exec_module(sys.modules[name])
    except BaseException:
        sys.modules.pop(name)
        raise


# ---------------------------------------------------------------------
# SubagentStop
# ---------------------------------------------------------------------


def decide_subagent_stop(marker_dir: Path, places: Places, session: str) -> Iterator[Decision]:
    """The accepted markers this session wrote whose agent finished with no report.

    Only this session's markers, since one agent finishing must not clear the
    marker of a run interrupted earlier or of a run in another worktree. A
    marker with no session line predates that line and is left for the TTL.
    """
    if not session:
        return
    for path in marker_paths(marker_dir):
        marker = Marker.read(path)
        accepted = path.name.startswith(ACCEPTED_PREFIX)
        if accepted and finished_without_report(marker, places, session):
            yield withhold_unreported(marker)


def finished_without_report(marker: Marker, places: Places, session: str) -> bool:
    """An inject marker waits on no report, and one with no bead id is Stop's to discard."""
    waiting = marker.mode != "inject" and marker.session == session and bool(marker.bead)
    waiting = waiting and marker.belongs_to(places.git_dir)
    return waiting and report_of_format("acceptance", marker, places) is None


def withhold_unreported(marker: Marker) -> Decision:
    return Decision(
        LOG,
        marker,
        remove=True,
        message=f"the {ACCEPTANCE_AGENT} agent finished and wrote no report for {marker.bead}",
        outcome=(
            f"withheld accepted, the {ACCEPTANCE_AGENT} agent finished and wrote no report; "
            f"the plugin cache can be older than {ACCEPTANCE_REPORT_SINCE}"
        ),
    )


# ---------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------


def report_after(marker: Marker, places: Places) -> Path | None:
    """The report of the marker's own run, or None while that run is still going."""
    return report_of_format(marker.report_format, marker, places)


def report_of_format(report_format: str, marker: Marker, places: Places) -> Path | None:
    name = REPORT_FILE_OF_FORMAT.get(report_format)
    if name is None:
        return newest_qa_report_after(marker, places.qa_dir)
    latest = places.git_dir / name
    if marker.run_id:
        return invocation_report(marker, latest, places.git_dir)
    return legacy_report(marker, latest)


def invocation_report(marker: Marker, latest: Path, git_dir: Path) -> Path | None:
    """The report that names the marker's run, else a version 2 report the legacy rule allows."""
    run_copy = git_dir / RUNS_DIRECTORY / marker.run_id / RUN_REPORT
    if named_run(run_copy) == marker.run_id:
        return run_copy
    latest_run = named_run(latest)
    if latest_run == marker.run_id:
        return latest
    return legacy_report(marker, latest) if latest_run is None else None


def legacy_report(marker: Marker, report: Path) -> Path | None:
    """A report that names no run describes this one only when it was written after the marker."""
    return report if report.is_file() and newer(report, marker.path) else None


def named_run(report: Path) -> str | None:
    """The run_id a report records, "" for a malformed one, or None when it records none.

    A version 2 report has no run_id field. A file that is missing or not a JSON
    object names no run either, and legacy_report then decides it as before.
    """
    fields = reports.read_json_object(report) or {}
    if "run_id" not in fields:
        return None
    value = fields["run_id"]
    return value if isinstance(value, str) else ""


def newest_qa_report_after(marker: Marker, qa_dir: Path) -> Path | None:
    candidates = [path for path in qa_reports(qa_dir) if newer(path, marker.path)]
    return max(candidates, key=mtime_ns, default=None)


def qa_reports(qa_dir: Path) -> list[Path]:
    """Regular files named qa-report-*.md directly in qa_dir; a symlink is not one."""
    try:
        entries = list(os.scandir(qa_dir))
    except OSError:
        return []
    return [
        Path(entry.path)
        for entry in entries
        if QA_REPORT.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)
    ]


def report_bead(report: Path, marker: Marker) -> str:
    """The bead the report names, or the marker's own when its format names none."""
    if marker.report_format not in BEAD_NAMING_FORMATS:
        return marker.bead
    bead = (reports.read_json_object(report) or {}).get("bead")
    if bead is None or bead is False:
        return ""
    return bead if isinstance(bead, str) else reports.render(bead)


def newer(path: Path, than: Path) -> bool:
    return mtime_ns(path) > mtime_ns(than)


def mtime_ns(path: Path) -> int:
    """The modification time, or -1 for a file that vanished, so it is never newer."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


# ---------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------


def marker_paths(marker_dir: Path) -> list[Path]:
    """Every marker in name order, skipping dotfiles and dangling links as the shell glob did."""
    try:
        paths = sorted(marker_dir.iterdir())
    except OSError:
        return []
    return [path for path in paths if not path.name.startswith(".") and path.exists()]


def read_marker_text(path: Path) -> str:
    """The marker's text, or nothing when it cannot be read.

    Read as bytes so a carriage return survives: "123\\r" is not a timestamp. A
    NUL is dropped, as the shell's command substitution dropped it.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace").replace("\0", "")


# ---------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------

USAGE = (
    "usage: resolve_stop_markers.py stop <marker-dir> <git-dir> <qa-reports-dir> <now> "
    "<head> <repo-root>\n"
    "       resolve_stop_markers.py subagent-stop <marker-dir> <git-dir> <session>"
)


def main(argv: list[str]) -> int:
    decisions = parse(argv)
    if decisions is None:
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE
    write_records(decisions, sys.stdout.buffer)
    return EXIT_OK


def parse(argv: list[str]) -> Iterable[Decision] | None:
    if len(argv) == 7 and argv[0] == "stop" and DIGITS.fullmatch(argv[4]):
        places = Places(Path(argv[2]), Path(argv[3]), int(argv[4]), argv[5], Path(argv[6]))
        return decide_stop(Path(argv[1]), places)
    if len(argv) == 4 and argv[0] == "subagent-stop":
        places = Places(Path(argv[2]), Path(), 0)
        return decide_subagent_stop(Path(argv[1]), places, argv[3])
    return None


def write_records(decisions: Iterable[Decision], stream) -> None:  # noqa: ANN001, a binary stream
    for decision in decisions:
        record = b"".join(encode(field) + b"\0" for field in decision.fields)
        stream.write(record)
        stream.flush()


def encode(field: str) -> bytes:
    """UTF-8, with a filename's undecodable bytes restored as they were read."""
    return field.replace("\0", "").encode("utf-8", errors="surrogateescape")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
