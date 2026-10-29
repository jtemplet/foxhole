#!/usr/bin/env python3
"""Decide whether one legacy report earns its pending bead label.

resolve_stop_markers.py finds the report a pending marker waits for, then asks
this module what the report adds up to. Four legacy formats, one
parser each (ADR 0011 keeps them separate):

    build           <git-dir>/build-report.json, written by /build
    quality-gates   <git-dir>/quality-gates-report.json, version 2
    acceptance      <git-dir>/acceptance-report.json, version 2
    qa              .gstack/qa-reports/qa-report-*.md, written by /qa

    python3 bead_label_reports.py <format> <report-path>

It prints one line, the reason the label script logs, and exits 0 when the
report passes, 1 when it does not, and 2 on a usage error. Every parser fails
closed: a report it cannot read is a refusal with a reason, never a crash, so
the Stop handler goes on to the next marker.

Which report and which bead stay in resolve_stop_markers.py, and the label
write stays in the label script. This module reads one file and decides; it
runs no git and no bd.

Stdlib only, and Python 3.9, since /usr/bin/python3 is what a consumer
repository is guaranteed to have.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

EXIT_PASS = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2

# What "everything passes" means for a /qa report, as the most Critical, High,
# and Deferred findings it may leave. /qa fixes what it finds, so demanding zero
# issues found would deny the label to a run that did its job. This asks instead
# that nothing serious and nothing unfinished remain.
QA_ROWS = ("Critical", "High", "Deferred")
QA_LIMITS = (0, 0, 0)

DIGITS = re.compile(r"[0-9]+")
# No real count needs more digits than this, and it fits a signed 64-bit integer.
MAX_DIGITS = 18
NOT_A_DIGIT = re.compile(r"[^0-9]")
TABLE_SPACE = r"[ \t\f\v\r]*"


@dataclass(frozen=True)
class Decision:
    passed: bool
    reason: str


def refuse(reason: str) -> Decision:
    return Decision(False, reason)


def accept(reason: str) -> Decision:
    return Decision(True, reason)


@dataclass(frozen=True)
class CountedFormat:
    """A JSON report whose verdict is derived from its counts, never read from it.

    Every field must be a count. Each rule is a refusal: when its test holds
    over the counts, the report is refused with its message. The rules run in
    order, so the reason names the first one that holds.
    """

    name: str
    fields: tuple[str, ...]
    rules: tuple[tuple[Callable[[dict[str, int]], bool], str], ...]
    passed: str

    def judge(self, path: Path, report: dict) -> Decision:
        counts, missing = read_counts(report, self.fields)
        if missing:
            return refuse(f"{self.name} {path} has no numeric {missing}; not labeling")
        for holds, message in self.rules:
            if holds(counts):
                return refuse(f"{self.name} {path} {message.format(**counts)}; not labeling")
        return accept(self.passed.format(path=path, **counts))


# ---------------------------------------------------------------------
# build-report.json
# ---------------------------------------------------------------------

# /build reports what it counted, and these rules decide what that means. A
# model that writes "verdict": "PASS" beside a failing test would otherwise
# label its own work.
BUILD = CountedFormat(
    name="build report",
    fields=(
        "criteria_met",
        "criteria_total",
        "tests_passed",
        "tests_failed",
        "lint_violations",
        "lint_ran",
    ),
    rules=(
        # "lint_violations: 0" is true both when the linter found nothing and
        # when it never ran. Only this flag tells them apart.
        (lambda c: not c["lint_ran"], "does not record lint_ran: true"),
        (lambda c: c["criteria_total"] == 0, "grades no criteria"),
        (
            lambda c: c["criteria_met"] != c["criteria_total"],
            "met {criteria_met} of {criteria_total} criteria",
        ),
        # Zero passing tests means the suite never ran or is empty, and then
        # tests_failed is zero too. Not `>= total`: one test can cover two criteria.
        (lambda c: c["tests_passed"] == 0, "records no passing tests"),
        (lambda c: c["tests_failed"] != 0, "has {tests_failed} failing test(s)"),
        (lambda c: c["lint_violations"] != 0, "has {lint_violations} lint violation(s)"),
    ),
    passed=(
        "build report passed ({criteria_met}/{criteria_total} criteria, {tests_passed} tests, "
        "0 failing, 0 violations) via {path}"
    ),
)


def build_report_passes(path: Path) -> Decision:
    report = read_json_object(path) or {}
    # lint_ran is a flag, not a count, so it is read as one before the counts.
    flagged = dict(report, lint_ran=int(is_true(report.get("lint_ran"))))
    return BUILD.judge(path, flagged)


# ---------------------------------------------------------------------
# quality-gates-report.json, version 2
# ---------------------------------------------------------------------


def quality_gates_report_passes(path: Path) -> Decision:
    """Only a verbatim PASS earns the label; FAIL, INCOMPLETE, and NO GATES RAN do not."""
    verdict = (read_json_object(path) or {}).get("verdict")
    if verdict is None or verdict is False or verdict == "":
        return refuse(f"could not read a verdict from {path}; not labeling")
    if verdict != "PASS":
        return refuse(f"quality-gates verdict {render(verdict)}; not labeling")
    return accept(f"quality-gates verdict PASS via {path}")


# ---------------------------------------------------------------------
# acceptance-report.json, version 2
# ---------------------------------------------------------------------

# All three criterion counts are checked, not only passed == total: a report
# claiming 9 of 9 passed AND 2 failed is malformed, and must not earn a label.
ACCEPTANCE = CountedFormat(
    name="acceptance report",
    fields=(
        "criteria_total",
        "criteria_passed",
        "criteria_failed",
        "criteria_unverifiable",
        "gates_total",
        "gates_failed",
        "gates_blocked",
    ),
    rules=(
        (lambda c: c["criteria_total"] == 0, "grades no criteria"),
        (
            lambda c: c["criteria_passed"] != c["criteria_total"],
            "passed {criteria_passed} of {criteria_total} criteria",
        ),
        (lambda c: c["criteria_failed"] != 0, "has {criteria_failed} failing criterion(s)"),
        # INCONCLUSIVE withholds the label just as NOT ACCEPTED does.
        (
            lambda c: c["criteria_unverifiable"] != 0,
            "has {criteria_unverifiable} unverifiable criterion(s)",
        ),
        # There is deliberately no gates_passed rule: the skill lets a SKIPPED
        # gate stand, so a run whose gates all skipped can still be ACCEPTED.
        (lambda c: c["gates_total"] == 0, "records no gates"),
        (lambda c: c["gates_failed"] != 0, "has {gates_failed} failing gate(s)"),
        (lambda c: c["gates_blocked"] != 0, "has {gates_blocked} blocked gate(s)"),
    ),
    passed=(
        "acceptance report passed ({criteria_passed}/{criteria_total} criteria, "
        "{gates_total} gates, 0 failing, 0 blocked) via {path}"
    ),
)


def acceptance_report_passes(path: Path) -> Decision:
    return ACCEPTANCE.judge(path, read_json_object(path) or {})


# ---------------------------------------------------------------------
# qa-report-*.md
# ---------------------------------------------------------------------


def qa_report_passes(path: Path) -> Decision:
    counts = qa_counts(read_text(path) or "")
    if counts is None:
        return refuse(f"could not parse severity counts from {path}; not labeling")
    summary = "critical={} high={} deferred={}".format(*counts)
    if any(count > limit for count, limit in zip(counts, QA_LIMITS)):  # noqa: B905, 3.10+
        return refuse(f"QA gate not met ({summary})")
    return accept(f"QA gate met ({summary}) via {path}")


def qa_counts(text: str) -> tuple[int, int, int] | None:
    """The Critical, High, and Deferred counts, or None without the first two."""
    critical, high, deferred = (qa_count(text, row) for row in QA_ROWS)
    if critical is None or high is None:
        return None
    # A report with no Ship Readiness block has nothing deferred.
    return critical, high, deferred or 0


def qa_count(text: str, row: str) -> int | None:
    """The count in the second cell of the first `| <row> | n |` table line.

    Every non-digit in that cell is dropped, as the shell's awk did, so
    "| Critical | 3 |" and "| **Critical** | 3 (fixed) |" both read as 3. An
    absent row or a cell with no digit reads as None.
    """
    line = first_row(text, row)
    if line is None:
        return None
    cells = line.split("|")
    return parse_digits(NOT_A_DIGIT.sub("", cells[2] if len(cells) > 2 else ""))


def first_row(text: str, row: str) -> str | None:
    pattern = re.compile(rf"^\|{TABLE_SPACE}\**{re.escape(row)}\**{TABLE_SPACE}\|", re.IGNORECASE)
    return next((line for line in text.split("\n") if pattern.match(line)), None)


# ---------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------


def read_json_object(path: Path) -> dict | None:
    """The report as a JSON object, or None when it is unreadable or not an object."""
    text = read_text(path)
    value = None if text is None else decode_json(text)
    return value if isinstance(value, dict) else None


def decode_json(text: str) -> object:
    """The decoded value, or None when the text is not JSON or nests past the stack."""
    try:
        return json.loads(text)
    except ValueError:
        return None
    except RecursionError:
        return None


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def read_counts(report: dict, fields: tuple[str, ...]) -> tuple[dict[str, int | None], str | None]:
    """The named counts, plus the first field that is not one, or None."""
    counts = {field: count_value(report.get(field)) for field in fields}
    missing = next((field for field, value in counts.items() if value is None), None)
    return counts, missing


def count_value(value: object) -> int | None:
    """A non-negative integer from a JSON integer or a string of digits.

    A digit string is read in base 10, so "08" is eight. The shell had to force
    that by hand, and a report claiming "08" failing tests once earned the label.
    A bool, a float, a negative number, and anything else read as None.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and DIGITS.fullmatch(value):
        return parse_digits(value)
    return None


def parse_digits(digits: str) -> int | None:
    """An ASCII digit string in base 10, or None when empty or longer than MAX_DIGITS.

    The cap keeps every interpreter agreeing: Python 3.11 raises ValueError past
    4300 digits where 3.9 converts, so an uncapped read would crash on one and
    pass on the other.
    """
    return int(digits, 10) if 0 < len(digits) <= MAX_DIGITS else None


def is_true(value: object) -> bool:
    # The shell compared jq's raw output to "true", so the string passes too.
    return value is True or value == "true"


def render(value: object) -> str:
    return value if isinstance(value, str) and value.isprintable() else json.dumps(value)


# ---------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------

PARSERS: dict[str, Callable[[Path], Decision]] = {
    "build": build_report_passes,
    "quality-gates": quality_gates_report_passes,
    "acceptance": acceptance_report_passes,
    "qa": qa_report_passes,
}

USAGE = f"usage: bead_label_reports.py {{{'|'.join(PARSERS)}}} <report-path>"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in PARSERS:
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE
    decision = PARSERS[argv[0]](Path(argv[1]))
    print(decision.reason)
    return EXIT_PASS if decision.passed else EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
