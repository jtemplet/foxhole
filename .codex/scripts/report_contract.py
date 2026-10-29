#!/usr/bin/env python3
"""Validate a quality-gates report, and decide what an automatic consumer may do with it.

This is the one validator of `quality-gates-report.json`
(docs/validation-report-contract.md). The writer, the Stop hook, the reconcile
loader, pre-push, acceptance reuse, and Outrigger all read the report through
it, so no two readers trust different fragments of one report.

THREE OPERATIONS, kept apart so each consumer applies its own policy:

  validate(report)                 shape, expected checks, evidence, and the
                                   verdict. Returns the derived verdict, or
                                   raises InvalidReport.
  currency_problem(report, facts)  the five "current" facts of the contract's
                                   Terms, checked against facts the caller kept.
  classify(text, facts, minimum)   both of the above for an automatic consumer:
                                   valid success, failure, incomplete, or not
                                   usable, with the manifest's reason.

EVERY FACT IS INJECTED. The caller passes the retained run ID, HEAD, bead, the
descriptor's `expected`, and the git directory. This module runs no git and no
bd (ADR 0011, rule 4). It reads only the files a report cites, under the git
directory the caller names.

DETERMINISTIC REFUSALS. Every refusal carries `rule`, the id of the rule in
docs/validation-report-fixtures/manifest.json that it breaks, and the checks run
in a fixed order, so the same report always gets the same reason.

VERSION 2 IS LEGACY. A version 2 report is checked against its frozen fields and
the Verdict Rules only. No reader fills in its missing identity or evidence, so
it is below the minimum of a strict consumer, and its freshness stays with the
legacy readers.

A quality-gates report carries `execution` and `observation` records. A
`citation` is how an acceptance report reuses a quality-gates result, so one in
a quality-gates report is refused.

Stdlib only, and Python 3.9, like the other portable helpers.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

KIND = "quality-gates"
REPORT_FILE = "quality-gates-report.json"
LEGACY_VERSION = 2
CURRENT_VERSION = 3

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"
BLOCKED = "BLOCKED"
HANDOFF = "HANDOFF"
INCOMPLETE = "INCOMPLETE"
NO_GATES_RAN = "NO GATES RAN"
GATE_STATUSES = (PASS, FAIL, WARN, SKIP, BLOCKED, HANDOFF)
VERDICTS = (PASS, FAIL, INCOMPLETE, NO_GATES_RAN)

VALID_SUCCESS = "valid-success"
VALID_FAILURE = "valid-failure"
VALID_INCOMPLETE = "valid-incomplete"
NOT_USABLE = "not-usable"
CLASS_OF_VERDICT = {
    PASS: VALID_SUCCESS,
    FAIL: VALID_FAILURE,
    INCOMPLETE: VALID_INCOMPLETE,
    NO_GATES_RAN: VALID_INCOMPLETE,
}

FINISHED = "finished"
NOT_RUN = "not_run"
EXECUTION_STATES = (FINISHED, "timed_out", "interrupted", NOT_RUN)
COMMAND_NOT_FOUND = 127
JOURNEY_OUTCOMES = ("VERIFY_PASS", "VERIFY_FAIL", "VERIFY_STALE", "VERIFY_BLOCKED")

MAX_EVIDENCE_LINES = 20
MAX_EVIDENCE_CHARS = 2000
TRIM_LINE = re.compile(r"\[trimmed: \d+ more lines\]")

COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
SHA256 = re.compile(r"[0-9a-f]{64}")
RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class InvalidReport(ValueError):
    """The report breaks a rule of the contract; `rule` names it in the manifest."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule


@dataclass(frozen=True)
class CallerFacts:
    """What an automatic consumer kept outside the report, measured by the caller."""

    run_id: str | None
    head: str
    bead: str | None
    expected: Any
    git_dir: Path
    repo_root: Path | None = None


@dataclass(frozen=True)
class Problem:
    """Why a valid report cannot serve this consumer: the manifest reason and rule."""

    reason: str
    rule: str
    detail: str


@dataclass(frozen=True)
class Decision:
    """The consumer class of one report, with the reason when it is not usable."""

    classification: str
    conclusion: str | None
    reason: str | None = None
    rule: str | None = None
    detail: str = ""


# ---------------------------------------------------------------- the consumer decision


def classify(text: str | None, facts: CallerFacts, minimum_version: int) -> Decision:
    """Sort the report file's text, or None for a missing file, into a consumer class.

    The order is fixed: missing, invalid, below the minimum, not current, then a
    bead that is not the caller's.
    """
    if minimum_version not in (LEGACY_VERSION, CURRENT_VERSION):
        raise ValueError(f"minimum_version is {minimum_version!r}, and it must be 2 or 3")
    if text is None:
        return Decision(NOT_USABLE, None, "missing", None, f"no {REPORT_FILE}")
    try:
        report = parse_report(text)
        verdict = validate(report)
    except InvalidReport as exc:
        return Decision(NOT_USABLE, None, "invalid", exc.rule, str(exc))
    if report["version"] < minimum_version:
        detail = f"version {report['version']} is below this consumer's minimum, {minimum_version}"
        return Decision(NOT_USABLE, verdict, "below-minimum", "minimum-version", detail)
    problem = currency_problem(report, facts) if report["version"] == CURRENT_VERSION else None
    if problem is None and report["bead"] != facts.bead:
        problem = Problem(
            "bead-mismatch",
            "bead-match",
            f"the report is for {report['bead']!r}, not {facts.bead!r}",
        )
    if problem is not None:
        return Decision(NOT_USABLE, verdict, problem.reason, problem.rule, problem.detail)
    return Decision(CLASS_OF_VERDICT[verdict], verdict)


def parse_report(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise InvalidReport("fields-exact", f"the report does not parse as JSON: {exc}") from exc


# ---------------------------------------------------------------- validation


def validate(report: Any) -> str:
    """The verdict the Verdict Rules give, once every rule of the report's version holds."""
    version = report_version(report)
    if version == LEGACY_VERSION:
        check_legacy_shape(report)
    else:
        check_shape(report)
        check_against_expected(report)
        check_evidence(report)
    check_verdict(report)
    return report["verdict"]


def report_version(report: Any) -> int:
    if not isinstance(report, dict):
        raise InvalidReport("fields-exact", f"the report is {type(report).__name__}, not an object")
    version = report.get("version")
    if not is_integer(version) or version not in (LEGACY_VERSION, CURRENT_VERSION):
        raise InvalidReport("version-integer", f"`version` is {version!r}, not the integer 3 or 2")
    return version


def check_verdict(report: dict[str, Any]) -> None:
    derived = derive_verdict(report["gates"])
    if report["verdict"] != derived:
        raise InvalidReport(
            "qa-verdict-rules",
            f"`verdict` is {report['verdict']}, and the Verdict Rules give {derived}",
        )


def derive_verdict(gates: list[dict[str, Any]]) -> str:
    """The Verdict Rules, in order: the first that matches decides."""
    statuses = {gate["status"] for gate in gates}
    if statuses & {FAIL, BLOCKED}:
        return FAIL
    if HANDOFF in statuses:
        return INCOMPLETE
    if not statuses & {PASS, WARN, FAIL}:
        return NO_GATES_RAN
    return PASS


# ---------------------------------------------------------------- shape


@dataclass(frozen=True)
class Rule:
    """What a field must hold, the words a refusal uses, and the manifest rule it serves."""

    holds: Callable[[Any], bool]
    expected: str
    rule: str = "fields-exact"


def is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def matches(pattern: re.Pattern[str]) -> Callable[[Any], bool]:
    return lambda value: isinstance(value, str) and pattern.fullmatch(value) is not None


def is_utc_timestamp(value: Any) -> bool:
    """Exactly YYYY-MM-DDTHH:MM:SSZ; the round trip refuses an unpadded field."""
    if not isinstance(value, str):
        return False
    try:
        return datetime.strptime(value, TIMESTAMP_FORMAT).strftime(TIMESTAMP_FORMAT) == value
    except ValueError:
        return False


def is_relative_path(value: Any) -> bool:
    """A path that stays under its root, so a cited file can never be read from elsewhere."""
    if not isinstance(value, str) or value == "" or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts


def within_cap(value: Any) -> bool:
    """At most 20 lines and 2,000 characters, before an optional trimmed line."""
    if value is None:
        return True
    lines = value.splitlines()
    if lines and TRIM_LINE.fullmatch(lines[-1]):
        lines = lines[:-1]
    return len(lines) <= MAX_EVIDENCE_LINES and len("\n".join(lines)) <= MAX_EVIDENCE_CHARS


TEXT = Rule(lambda value: isinstance(value, str), "a string")
NON_EMPTY_TEXT = Rule(lambda value: isinstance(value, str) and value != "", "a non-empty string")
OPTIONAL_TEXT = Rule(lambda value: value is None or isinstance(value, str), "a string or null")
OBJECT = Rule(lambda value: isinstance(value, dict), "an object")
OPTIONAL_OBJECT = Rule(lambda value: value is None or isinstance(value, dict), "an object or null")
LIST = Rule(lambda value: isinstance(value, list), "a list")
BOOLEAN = Rule(lambda value: isinstance(value, bool), "true or false")
COUNT = Rule(lambda value: is_integer(value) and value >= 0, "a non-negative integer")
OPTIONAL_COUNT = Rule(
    lambda value: value is None or (is_integer(value) and value >= 0),
    "a non-negative integer or null",
)
TEXT_LIST = Rule(
    lambda value: isinstance(value, list) and all(isinstance(item, str) for item in value),
    "a list of strings",
)
OPTIONAL_TEXT_LIST = Rule(
    lambda value: value is None or TEXT_LIST.holds(value), "a list of strings or null"
)
SHA = Rule(matches(SHA256), "64 lowercase hexadecimal digits")
CAPPED_TEXT = Rule(
    lambda value: (value is None or isinstance(value, str)) and within_cap(value),
    "at most 20 lines and 2,000 characters before a trimmed line",
    "evidence-text-cap",
)
RELATIVE_PATH = Rule(is_relative_path, "a relative path that stays under its root")
VERDICT = Rule(lambda value: value in VERDICTS, "one of " + ", ".join(VERDICTS))
SCOPE = Rule(lambda value: value in ("changed", "all"), "`changed` or `all`")
STATUS = Rule(lambda value: value in GATE_STATUSES, "one of " + ", ".join(GATE_STATUSES))

LEGACY_FIELDS = {
    "version": COUNT,
    "head": Rule(matches(re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")), "a hex commit id"),
    "dirty": BOOLEAN,
    "timestamp": Rule(is_utc_timestamp, "UTC YYYY-MM-DDTHH:MM:SSZ", "timestamp-utc"),
    "scope": SCOPE,
    "gate_source": NON_EMPTY_TEXT,
    "routing": OBJECT,
    "bead": OPTIONAL_TEXT,
    "base": OPTIONAL_OBJECT,
    "changed_files": OPTIONAL_TEXT_LIST,
    "verdict": VERDICT,
    "gates": LIST,
    "findings": LIST,
}
FIELDS = {
    **LEGACY_FIELDS,
    "kind": Rule(
        lambda value: value == KIND, f"`{KIND}`, the file the reader opened", "kind-matches-file"
    ),
    "run_id": Rule(matches(RUN_ID), "a lowercase version 4 UUID", "run-id-format"),
    "producer": OBJECT,
    "head": Rule(matches(COMMIT), "40 or 64 lowercase hexadecimal digits", "commit-format"),
    "bead": Rule(
        lambda value: value is None or (isinstance(value, str) and value != ""),
        "a non-empty bead id or null",
        "bead-nonempty-or-null",
    ),
    "expected": OBJECT,
    "evidence": LIST,
}
PRODUCER_FIELDS = {"name": NON_EMPTY_TEXT, "version": NON_EMPTY_TEXT}
BASE_FIELDS = {"ref": TEXT, "sha": TEXT}
ROUTE_FIELDS = {"method": TEXT, "owner": OPTIONAL_TEXT, "files": COUNT, "endpoints": COUNT}
LEGACY_GATE_FIELDS = {
    "name": NON_EMPTY_TEXT,
    "status": STATUS,
    "command": OPTIONAL_TEXT,
    "detail": TEXT,
}
GATE_FIELDS = {**LEGACY_GATE_FIELDS, "evidence_ids": TEXT_LIST}
FINDING_FIELDS = {
    "gate": TEXT,
    "file": OPTIONAL_TEXT,
    "line": Rule(
        lambda value: value is None or (is_integer(value) and value >= 1),
        "an integer of 1 or more, or null",
    ),
    "problem": NON_EMPTY_TEXT,
    "evidence": CAPPED_TEXT,
}
EXPECTED_FIELDS = {"checks": LIST}
CHECK_FIELDS = {
    "name": NON_EMPTY_TEXT,
    "applies": BOOLEAN,
    "reason": OPTIONAL_TEXT,
    "evidence_kind": Rule(
        lambda value: value in ("execution", "observation"), "`execution` or `observation`"
    ),
    "pass_exit_codes": Rule(
        lambda value: (
            value is None
            or (isinstance(value, list) and value != [] and all(is_integer(code) for code in value))
        ),
        "a non-empty list of integers, or null",
    ),
    "ui": OPTIONAL_OBJECT,
}
UI_FIELDS = {"surface": NON_EMPTY_TEXT, "journeys": LIST, "uncovered_files": TEXT_LIST}
JOURNEY_FIELDS = {"name": NON_EMPTY_TEXT, "fingerprint": NON_EMPTY_TEXT}
EXECUTION_FIELDS = {
    "id": NON_EMPTY_TEXT,
    "kind": TEXT,
    "command": TEXT,
    "cwd": NON_EMPTY_TEXT,
    "state": Rule(lambda value: value in EXECUTION_STATES, "one of " + ", ".join(EXECUTION_STATES)),
    "exit_code": Rule(lambda value: value is None or is_integer(value), "an integer or null"),
    "duration_ms": OPTIONAL_COUNT,
    "output": OPTIONAL_OBJECT,
    "inputs": OBJECT,
}
OUTPUT_FIELDS = {
    "path": RELATIVE_PATH,
    "sha256": SHA,
    "bytes": COUNT,
    "excerpt": CAPPED_TEXT,
    "omitted_lines": COUNT,
}
INPUTS_FIELDS = {"digest": SHA, "stable": BOOLEAN}
OBSERVATION_FIELDS = {
    "id": NON_EMPTY_TEXT,
    "kind": TEXT,
    "subject": Rule(
        lambda value: value in ("code", "journey", "artifact"), "`code`, `journey`, or `artifact`"
    ),
    "ref": NON_EMPTY_TEXT,
    "revision": Rule(matches(COMMIT), "40 or 64 lowercase hexadecimal digits", "commit-format"),
    "artifact": OPTIONAL_OBJECT,
    "fingerprint": OPTIONAL_TEXT,
    "outcome": Rule(
        lambda value: value is None or value in JOURNEY_OUTCOMES,
        "one of " + ", ".join(JOURNEY_OUTCOMES) + ", or null",
    ),
}
ARTIFACT_FIELDS = {
    "root": Rule(lambda value: value in ("git-dir", "repo"), "`git-dir` or `repo`"),
    "path": RELATIVE_PATH,
    "sha256": SHA,
}


def check_legacy_shape(report: dict[str, Any]) -> None:
    """The frozen version 2 fields: no version 3 field is filled in or accepted."""
    check_entry(report, LEGACY_FIELDS, "the report", unexpected_rule="legacy-frozen")
    check_common_shape(report, LEGACY_GATE_FIELDS)


def check_shape(report: dict[str, Any]) -> None:
    # `kind` first, so an acceptance report opened here names the file mismatch.
    if report.get("kind") != KIND:
        raise InvalidReport("kind-matches-file", f"`kind` is {report.get('kind')!r}, not `{KIND}`")
    check_entry(report, FIELDS, "the report")
    check_entry(report["producer"], PRODUCER_FIELDS, "`producer`")
    check_common_shape(report, GATE_FIELDS)
    check_entry(report["expected"], EXPECTED_FIELDS, "`expected`")
    for index, check in enumerate(report["expected"]["checks"]):
        check_expected_check(check, f"`expected.checks[{index}]`")
    for index, record in enumerate(report["evidence"]):
        check_record(record, f"`evidence[{index}]`")


def check_common_shape(report: dict[str, Any], gate_fields: dict[str, Rule]) -> None:
    if report["base"] is not None:
        check_entry(report["base"], BASE_FIELDS, "`base`")
    if (report["base"] is None) != (report["changed_files"] is None):
        raise InvalidReport(
            "base-changed-pair", "`base` and `changed_files` must both be null, or both set"
        )
    for surface, route in report["routing"].items():
        check_entry(route, ROUTE_FIELDS, f"`routing.{surface}`")
    for index, gate in enumerate(report["gates"]):
        check_entry(gate, gate_fields, f"`gates[{index}]`")
    names = [gate["name"] for gate in report["gates"]]
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise InvalidReport("unique-results", f"the gate names {repeated} repeat")
    for index, finding in enumerate(report["findings"]):
        where = f"`findings[{index}]`"
        check_entry(finding, FINDING_FIELDS, where)
        if finding["gate"] not in names:
            raise InvalidReport(
                "fields-exact", f"{where} names the gate `{finding['gate']}`, which has no row"
            )


def check_expected_check(check: Any, where: str) -> None:
    check_entry(check, CHECK_FIELDS, where)
    if check["applies"] == (check["reason"] is not None) or check["reason"] == "":
        raise InvalidReport(
            "fields-exact", f"{where} needs a non-empty `reason` exactly when `applies` is false"
        )
    if (check["evidence_kind"] == "execution") != (check["pass_exit_codes"] is not None):
        raise InvalidReport(
            "fields-exact",
            f"{where} needs `pass_exit_codes` exactly when `evidence_kind` is execution",
        )
    if check["ui"] is not None:
        check_entry(check["ui"], UI_FIELDS, f"{where}.ui")
        for index, journey in enumerate(check["ui"]["journeys"]):
            check_entry(journey, JOURNEY_FIELDS, f"{where}.ui.journeys[{index}]")
        names = [journey["name"] for journey in check["ui"]["journeys"]]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise InvalidReport("fields-exact", f"{where}.ui names the journeys {repeated} twice")


def check_record(record: Any, where: str) -> None:
    kind = record.get("kind") if isinstance(record, dict) else None
    if kind == "execution":
        check_execution_record(record, where)
    elif kind == "observation":
        check_observation_record(record, where)
    else:
        raise InvalidReport(
            "fields-exact",
            f"{where} has the kind {kind!r}; it must be `execution` or `observation`",
        )


def check_execution_record(record: dict[str, Any], where: str) -> None:
    check_entry(record, EXECUTION_FIELDS, where)
    check_entry(record["inputs"], INPUTS_FIELDS, f"{where}.inputs")
    if record["output"] is not None:
        check_entry(record["output"], OUTPUT_FIELDS, f"{where}.output")
    not_run = record["state"] == NOT_RUN
    consistent = (
        (record["exit_code"] is not None) == (record["state"] == FINISHED)
        and (record["duration_ms"] is None) == not_run
        and (record["output"] is None) == not_run
    )
    if not consistent:
        raise InvalidReport(
            "execution-record-state",
            f"{where} is {record['state']}: only a finished record has an exit code, "
            "and only a not_run record lacks a duration and output",
        )


def check_observation_record(record: dict[str, Any], where: str) -> None:
    check_entry(record, OBSERVATION_FIELDS, where)
    if (record["subject"] == "code") != (record["artifact"] is None):
        raise InvalidReport(
            "fields-exact", f"{where} needs an `artifact` exactly when its subject is not code"
        )
    if record["artifact"] is not None:
        check_entry(record["artifact"], ARTIFACT_FIELDS, f"{where}.artifact")
    journey = record["subject"] == "journey"
    if journey != (record["fingerprint"] is not None) or journey != (record["outcome"] is not None):
        raise InvalidReport(
            "fields-exact",
            f"{where} needs `fingerprint` and `outcome` exactly when its subject is journey",
        )


def check_entry(
    entry: Any, rules: dict[str, Rule], where: str, unexpected_rule: str = "fields-exact"
) -> None:
    """Refuse a non-object, a missing field, an unexpected field, or a bad value."""
    if not isinstance(entry, dict):
        raise InvalidReport("fields-exact", f"{where} is {type(entry).__name__}, not an object")
    for name in rules:
        if name not in entry:
            raise InvalidReport("fields-exact", f"{where} lacks the field `{name}`")
    for name in entry:
        if name not in rules:
            raise InvalidReport(unexpected_rule, f"{where} holds the unexpected field `{name}`")
    for name, rule in rules.items():
        if not rule.holds(entry[name]):
            raise InvalidReport(rule.rule, f"{where} field `{name}` must be {rule.expected}")


# ---------------------------------------------------------------- results against expected inputs


def check_against_expected(report: dict[str, Any]) -> None:
    """One result per expected check, by name and in order; a check that does not apply is SKIP."""
    expected = [check["name"] for check in report["expected"]["checks"]]
    results = [gate["name"] for gate in report["gates"]]
    if results != expected:
        raise InvalidReport(
            "checks-match-expected",
            f"the gate results {results} differ from the expected checks {expected}",
        )
    for check, gate in checks_with_results(report):
        if not check["applies"] and (gate["status"] != SKIP or gate["evidence_ids"]):
            raise InvalidReport(
                "applies-false-skip",
                f"`{gate['name']}` does not apply, so it must be SKIP with no evidence",
            )


def checks_with_results(report: dict[str, Any]):
    # Python 3.9 has no zip(strict=); the names were already matched one to one.
    return zip(report["expected"]["checks"], report["gates"])  # noqa: B905


# ---------------------------------------------------------------- evidence


def check_evidence(report: dict[str, Any]) -> None:
    records = records_by_id(report["evidence"])
    for check, gate in checks_with_results(report):
        cited = []
        for evidence_id in gate["evidence_ids"]:
            if evidence_id not in records:
                raise InvalidReport(
                    "evidence-ids-resolve",
                    f"`{gate['name']}` cites `{evidence_id}`, which no record holds",
                )
            cited.append(records[evidence_id])
        if check["ui"] is not None and gate["status"] == SKIP:
            raise InvalidReport(
                "ui-never-skip", f"`{gate['name']}` is required UI work, so it is never SKIP"
            )
        if gate["status"] == SKIP:
            if cited:
                raise InvalidReport(
                    "skip-no-evidence", f"`{gate['name']}` is SKIP and cites evidence"
                )
        elif check["ui"] is not None:
            check_ui_status(check, gate, cited)
        elif check["evidence_kind"] == "execution":
            check_execution_status(check, gate, cited)
        else:
            check_observation_status(gate, cited, report["head"])


def records_by_id(evidence: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for record in evidence:
        if record["id"] in records:
            raise InvalidReport("evidence-id-unique", f"two records carry the id `{record['id']}`")
        records[record["id"]] = record
    return records


def check_execution_status(
    check: dict[str, Any], gate: dict[str, Any], cited: list[dict[str, Any]]
) -> None:
    """The contract's "What a result must cite" table, for a check a command decides."""
    status, name = gate["status"], gate["name"]
    runs = [record for record in cited if record["kind"] == "execution"]
    finished = [record for record in runs if record["state"] == FINISHED]
    if status == PASS:
        passing = [
            r
            for r in finished
            if r["exit_code"] in check["pass_exit_codes"] and r["inputs"]["stable"]
        ]
        if not cited or len(passing) != len(cited):
            unstable = any(not record["inputs"]["stable"] for record in runs)
            rule = "handoff-unstable-inputs" if unstable else "pass-evidence"
            raise InvalidReport(
                rule,
                f"`{name}` is PASS, so every record it cites must finish stable with an exit in "
                f"{check['pass_exit_codes']}",
            )
    elif status == WARN:
        if not cited or len([r for r in finished if r["inputs"]["stable"]]) != len(cited):
            unstable = any(not record["inputs"]["stable"] for record in runs)
            raise InvalidReport(
                "handoff-unstable-inputs" if unstable else "warn-evidence",
                f"`{name}` is WARN, so every record it cites must finish stable",
            )
    elif status == FAIL:
        if not finished:
            raise InvalidReport("fail-evidence", f"`{name}` is FAIL and cites no finished record")
    elif status == BLOCKED:
        if not any(r["state"] != FINISHED or r["exit_code"] == COMMAND_NOT_FOUND for r in runs):
            raise InvalidReport(
                "blocked-evidence",
                f"`{name}` is BLOCKED and cites no record that stopped or exited 127",
            )


def check_observation_status(gate: dict[str, Any], cited: list[dict[str, Any]], head: str) -> None:
    """Without a UI spec, PASS, WARN, and FAIL each cite an observation of the checked commit."""
    if gate["status"] not in (PASS, WARN, FAIL):
        return
    if not any(record["kind"] == "observation" and record["revision"] == head for record in cited):
        rule = {PASS: "pass-evidence", WARN: "warn-evidence", FAIL: "fail-evidence"}[gate["status"]]
        raise InvalidReport(
            rule, f"`{gate['name']}` is {gate['status']} and cites no observation at `head`"
        )


def check_ui_status(
    check: dict[str, Any], gate: dict[str, Any], cited: list[dict[str, Any]]
) -> None:
    derived = ui_status(check["ui"], cited)
    if gate["status"] != derived:
        # A PASS or FAIL lacks its own proof; any other status hides what was found.
        broken = gate["status"] if gate["status"] in (PASS, FAIL) else derived
        rule = {
            FAIL: "ui-precedence-fail",
            HANDOFF: "ui-precedence-handoff",
            PASS: "ui-precedence-pass",
        }[broken]
        raise InvalidReport(
            rule, f"`{gate['name']}` is {gate['status']}, and its journeys give {derived}"
        )


def ui_status(ui: dict[str, Any], cited: list[dict[str, Any]]) -> str:
    """The UI precedence: a current failure, then anything unfinished, then a complete pass.

    A journey result is current only when its fingerprint equals the expected one.
    """
    fingerprints = {journey["name"]: journey["fingerprint"] for journey in ui["journeys"]}
    current = [
        record
        for record in cited
        if record["kind"] == "observation"
        and record["subject"] == "journey"
        and fingerprints.get(record["ref"]) == record["fingerprint"]
    ]
    if any(record["outcome"] == "VERIFY_FAIL" for record in current):
        return FAIL
    passed = {record["ref"] for record in current if record["outcome"] == "VERIFY_PASS"}
    if passed == set(fingerprints) and not ui["uncovered_files"]:
        return PASS
    return HANDOFF


# ---------------------------------------------------------------- currency


def currency_problem(report: dict[str, Any], facts: CallerFacts) -> Problem | None:
    """The first of the five "current" facts that fails, or None when the report is current.

    `dirty` comes before `head`: a dirty report is never current, whatever HEAD
    later becomes. `report` must already have passed validate() at version 3.
    """
    if report["run_id"] != facts.run_id:
        return not_current(
            "current-run-id", f"the report is run {report['run_id']}, not {facts.run_id}"
        )
    if report["expected"] != facts.expected:
        return not_current("current-expected", "`expected` differs from the descriptor's copy")
    if report["dirty"]:
        return not_current("current-dirty", "the report checked uncommitted work")
    if report["head"] != facts.head:
        return not_current(
            "current-head", f"the report checked {report['head']}, and HEAD is {facts.head}"
        )
    for path, digest in cited_files(report, facts):
        if path is None or file_digest(path) != digest:
            return not_current(
                "current-digests", f"{path} is missing or no longer matches its sha256"
            )
    return None


def not_current(rule: str, detail: str) -> Problem:
    return Problem("not-current", rule, detail)


def cited_files(report: dict[str, Any], facts: CallerFacts):
    """Each cited file's path, or None when its root is unknown, with its recorded sha256."""
    run_directory = facts.git_dir / "validation-runs" / report["run_id"]
    for record in report["evidence"]:
        if record["kind"] == "execution" and record["output"] is not None:
            yield run_directory / record["output"]["path"], record["output"]["sha256"]
        if record["kind"] == "observation" and record["artifact"] is not None:
            artifact = record["artifact"]
            root = facts.git_dir if artifact["root"] == "git-dir" else facts.repo_root
            yield (None if root is None else root / artifact["path"]), artifact["sha256"]


def file_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
