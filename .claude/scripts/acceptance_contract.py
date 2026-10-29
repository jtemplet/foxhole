#!/usr/bin/env python3
"""Validate an acceptance report, and decide what an automatic consumer may do with it.

This is the one validator of `acceptance-report.json`
(docs/validation-report-contract.md). The writer, the Stop hook, the reconcile
loader, and Outrigger all read the report through it, so no two readers trust
different fragments of one report.

THREE OPERATIONS, the same three the quality-gates validator offers:

  validate(report)                 shape, expected checks and criteria, evidence,
                                   and the counts. Returns the derived conclusion,
                                   or raises InvalidReport.
  currency_problem(report, facts)  the five "current" facts of the contract's
                                   Terms, plus every citation's six conditions,
                                   checked against facts the caller kept.
  classify(text, facts, minimum)   both of the above for an automatic consumer:
                                   valid success, failure, incomplete, or not
                                   usable, with the manifest's reason.

THERE IS NO VERDICT FIELD. The conclusion is derived from the counts, and the
counts are checked against the rows, so a report cannot read ACCEPTED over a
criterion it graded FAIL. A top-level `verdict` is refused before anything else.

THE NATIVE CRITERIA ARE AUTHORITATIVE (ADR 0001). Each expected criterion has
exactly one result with the same number and the same text, in the same order.
Grading never adds, drops, or edits one.

EVERY FACT IS INJECTED (ADR 0011, rule 4). The caller passes the retained run
ID, HEAD, bead, the descriptor's `expected`, and the git directory. This module
runs no git and no bd. It reads only the files a report cites, under the
directory the caller names.

THE SHARED PIECES COME FROM report_contract.py. The field rules, the record
shapes, the UI precedence, and the consumer classes are the quality-gates
validator's, and a citation must name a valid quality-gates report, so this
module imports that one. It looks beside itself first, where an installer puts
both, and then in the plugin's own layout.

Stdlib only, and Python 3.9, like the other portable helpers.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def load_report_contract() -> ModuleType:
    if "report_contract" in sys.modules:
        return sys.modules["report_contract"]
    here = Path(__file__).resolve().parent
    for directory in (here, here.parents[1] / "quality-gates" / "scripts"):
        path = directory / "report_contract.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("report_contract", path)
            module = importlib.util.module_from_spec(spec)
            # A dataclass resolves its module through sys.modules while it is built.
            sys.modules["report_contract"] = module
            spec.loader.exec_module(module)
            return module
    raise ImportError(f"report_contract.py is neither beside {here} nor in the plugin layout")


qg = load_report_contract()
Rule = qg.Rule
InvalidReport = qg.InvalidReport
CallerFacts = qg.CallerFacts
Decision = qg.Decision
Problem = qg.Problem
check_entry = qg.check_entry

KIND = "acceptance"
REPORT_FILE = "acceptance-report.json"
LEGACY_VERSION = qg.LEGACY_VERSION
CURRENT_VERSION = qg.CURRENT_VERSION

PASS = qg.PASS
FAIL = qg.FAIL
SKIP = qg.SKIP
BLOCKED = qg.BLOCKED
HANDOFF = qg.HANDOFF
UNVERIFIABLE = "UNVERIFIABLE"
CRITERION_VERDICTS = (PASS, FAIL, UNVERIFIABLE)
LEGACY_GATE_STATUSES = (PASS, FAIL, BLOCKED, SKIP)
GATE_STATUSES = (*LEGACY_GATE_STATUSES, HANDOFF)

ACCEPTED = "ACCEPTED"
NOT_ACCEPTED = "NOT ACCEPTED"
INCONCLUSIVE = "INCONCLUSIVE"
CLASS_OF_CONCLUSION = {
    ACCEPTED: qg.VALID_SUCCESS,
    NOT_ACCEPTED: qg.VALID_FAILURE,
    INCONCLUSIVE: qg.VALID_INCOMPLETE,
}
NOT_USABLE = qg.NOT_USABLE

CRITERION_SOURCES = ("acceptance_criteria", "notes", "injected")
LEGACY_COUNT_FIELDS = (
    "criteria_total",
    "criteria_passed",
    "criteria_failed",
    "criteria_unverifiable",
    "gates_total",
    "gates_failed",
    "gates_blocked",
)
COUNT_FIELDS = (*LEGACY_COUNT_FIELDS, "gates_handoff")


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
        report = qg.parse_report(text)
        conclusion = validate(report)
    except InvalidReport as exc:
        return Decision(NOT_USABLE, None, "invalid", exc.rule, str(exc))
    if report["version"] < minimum_version:
        detail = f"version {report['version']} is below this consumer's minimum, {minimum_version}"
        return Decision(NOT_USABLE, conclusion, "below-minimum", "minimum-version", detail)
    problem = currency_problem(report, facts) if report["version"] == CURRENT_VERSION else None
    if problem is None and report["bead"] != facts.bead:
        problem = Problem(
            "bead-mismatch",
            "bead-match",
            f"the report is for {report['bead']!r}, not {facts.bead!r}",
        )
    if problem is not None:
        return Decision(NOT_USABLE, conclusion, problem.reason, problem.rule, problem.detail)
    return Decision(CLASS_OF_CONCLUSION[conclusion], conclusion)


# ---------------------------------------------------------------- validation


def validate(report: Any) -> str:
    """The conclusion the counts give, once every rule of the report's version holds."""
    version = qg.report_version(report)
    if version == LEGACY_VERSION:
        check_no_verdict(report)
        check_legacy_shape(report)
    else:
        check_shape(report)
        check_against_expected(report)
        check_evidence(report)
    check_counts(report)
    return derive_conclusion(report)


def check_no_verdict(report: dict[str, Any]) -> None:
    """A conclusion written beside the rows would be the run grading its own work."""
    if "verdict" in report:
        raise InvalidReport(
            "acceptance-no-verdict",
            "the report holds a top-level `verdict`; the counts decide the conclusion",
        )


def check_counts(report: dict[str, Any]) -> None:
    for name, tally in tallies_of(report).items():
        if report[name] != tally:
            raise InvalidReport(
                "acceptance-counts", f"`{name}` is {report[name]}, and the rows count {tally}"
            )


def tallies_of(report: dict[str, Any]) -> dict[str, int]:
    """What each count must be; version 2 has no `gates_handoff`."""
    criteria = [row["verdict"] for row in report["criteria"]]
    gates = [row["status"] for row in report["gates"]]
    tallies = {
        "criteria_total": len(criteria),
        "criteria_passed": criteria.count(PASS),
        "criteria_failed": criteria.count(FAIL),
        "criteria_unverifiable": criteria.count(UNVERIFIABLE),
        "gates_total": len(gates),
        "gates_failed": gates.count(FAIL),
        "gates_blocked": gates.count(BLOCKED),
    }
    if report["version"] == CURRENT_VERSION:
        tallies["gates_handoff"] = gates.count(HANDOFF)
    return tallies


def derive_conclusion(report: dict[str, Any]) -> str:
    """The contract's Derived conclusions, in order; an all-SKIP gate list still accepts."""
    if report["criteria_failed"] or report["gates_failed"] or report["gates_blocked"]:
        return NOT_ACCEPTED
    if (
        report["criteria_unverifiable"]
        or report.get("gates_handoff", 0)
        or not report["criteria_total"]
        or not report["gates_total"]
    ):
        return INCONCLUSIVE
    return ACCEPTED


# ---------------------------------------------------------------- shape

COUNT = qg.COUNT
CRITERION_NUMBER = Rule(
    lambda value: qg.is_integer(value) and value >= 1, "an integer of 1 or more"
)
CRITERION_VERDICT = Rule(
    lambda value: value in CRITERION_VERDICTS, "one of " + ", ".join(CRITERION_VERDICTS)
)
LEGACY_STATUS = Rule(
    lambda value: value in LEGACY_GATE_STATUSES, "one of " + ", ".join(LEGACY_GATE_STATUSES)
)
STATUS = Rule(
    lambda value: value in GATE_STATUSES,
    "one of " + ", ".join(GATE_STATUSES),
    "acceptance-status-set",
)

LEGACY_FIELDS = {
    "version": COUNT,
    "head": qg.LEGACY_FIELDS["head"],
    "bead": qg.FIELDS["bead"],
    "base": qg.OPTIONAL_OBJECT,
    "changed_files": qg.OPTIONAL_TEXT_LIST,
    **dict.fromkeys(LEGACY_COUNT_FIELDS, COUNT),
    "criteria": qg.LIST,
    "gates": qg.LIST,
}
FIELDS = {
    **LEGACY_FIELDS,
    "kind": Rule(
        lambda value: value == KIND, f"`{KIND}`, the file the reader opened", "kind-matches-file"
    ),
    "run_id": qg.FIELDS["run_id"],
    "producer": qg.OBJECT,
    "timestamp": qg.FIELDS["timestamp"],
    "head": qg.FIELDS["head"],
    "dirty": qg.BOOLEAN,
    "expected": qg.OBJECT,
    "evidence": qg.LIST,
    "gates_handoff": COUNT,
}
LEGACY_CRITERION_FIELDS = {
    "number": CRITERION_NUMBER,
    "text": qg.TEXT,
    "verdict": CRITERION_VERDICT,
    "evidence": qg.CAPPED_TEXT,
    "command": qg.OPTIONAL_TEXT,
}
CRITERION_FIELDS = {**LEGACY_CRITERION_FIELDS, "evidence_ids": qg.TEXT_LIST}
LEGACY_GATE_FIELDS = {**qg.LEGACY_GATE_FIELDS, "status": LEGACY_STATUS}
GATE_FIELDS = {**qg.GATE_FIELDS, "status": STATUS}
EXPECTED_FIELDS = {"checks": qg.LIST, "criteria": qg.LIST}
EXPECTED_CRITERION_FIELDS = {
    "number": CRITERION_NUMBER,
    "text": qg.NON_EMPTY_TEXT,
    "source": Rule(
        lambda value: value in CRITERION_SOURCES, "one of " + ", ".join(CRITERION_SOURCES)
    ),
}
CITATION_FIELDS = {
    "id": qg.NON_EMPTY_TEXT,
    "kind": qg.TEXT,
    "report_kind": Rule(lambda value: value == qg.KIND, f"`{qg.KIND}`"),
    "run_id": qg.FIELDS["run_id"],
    "evidence_id": qg.NON_EMPTY_TEXT,
    "report_sha256": qg.SHA,
    "inputs_digest": qg.SHA,
}


def check_legacy_shape(report: dict[str, Any]) -> None:
    """The frozen version 2 fields: no version 3 field is filled in or accepted."""
    check_entry(report, LEGACY_FIELDS, "the report", unexpected_rule="legacy-frozen")
    check_common_shape(report, LEGACY_CRITERION_FIELDS, LEGACY_GATE_FIELDS)


def check_shape(report: dict[str, Any]) -> None:
    # `kind` first, so a quality-gates report opened here names the file mismatch.
    if report.get("kind") != KIND:
        raise InvalidReport("kind-matches-file", f"`kind` is {report.get('kind')!r}, not `{KIND}`")
    check_no_verdict(report)
    check_entry(report, FIELDS, "the report")
    check_entry(report["producer"], qg.PRODUCER_FIELDS, "`producer`")
    check_common_shape(report, CRITERION_FIELDS, GATE_FIELDS)
    check_entry(report["expected"], EXPECTED_FIELDS, "`expected`")
    for index, check in enumerate(report["expected"]["checks"]):
        qg.check_expected_check(check, f"`expected.checks[{index}]`")
    for index, criterion in enumerate(report["expected"]["criteria"]):
        check_entry(criterion, EXPECTED_CRITERION_FIELDS, f"`expected.criteria[{index}]`")
    for index, record in enumerate(report["evidence"]):
        check_record(record, f"`evidence[{index}]`")


def check_common_shape(
    report: dict[str, Any], criterion_fields: dict[str, Rule], gate_fields: dict[str, Rule]
) -> None:
    if report["base"] is not None:
        check_entry(report["base"], qg.BASE_FIELDS, "`base`")
    if (report["base"] is None) != (report["changed_files"] is None):
        raise InvalidReport(
            "base-changed-pair", "`base` and `changed_files` must both be null, or both set"
        )
    for index, criterion in enumerate(report["criteria"]):
        check_entry(criterion, criterion_fields, f"`criteria[{index}]`")
    for index, gate in enumerate(report["gates"]):
        check_entry(gate, gate_fields, f"`gates[{index}]`")
    refuse_repeats([criterion["number"] for criterion in report["criteria"]], "criterion numbers")
    refuse_repeats([gate["name"] for gate in report["gates"]], "gate names")


def refuse_repeats(values: list[Any], what: str) -> None:
    repeated = sorted({value for value in values if values.count(value) > 1})
    if repeated:
        raise InvalidReport("unique-results", f"the {what} {repeated} repeat")


def check_record(record: Any, where: str) -> None:
    kind = record.get("kind") if isinstance(record, dict) else None
    if kind == "execution":
        qg.check_execution_record(record, where)
    elif kind == "observation":
        qg.check_observation_record(record, where)
    elif kind == "citation":
        check_entry(record, CITATION_FIELDS, where)
    else:
        raise InvalidReport(
            "fields-exact",
            f"{where} has the kind {kind!r}; it must be `execution`, `observation`, or `citation`",
        )


# ---------------------------------------------------------------- results against expected inputs


def check_against_expected(report: dict[str, Any]) -> None:
    """One result per expected check and per expected criterion, in order and unedited."""
    qg.check_against_expected(report)
    expected = [(c["number"], c["text"]) for c in report["expected"]["criteria"]]
    results = [(c["number"], c["text"]) for c in report["criteria"]]
    if results != expected:
        raise InvalidReport(
            "criteria-match-expected",
            f"the criterion results {results} differ from the expected criteria {expected}",
        )


# ---------------------------------------------------------------- evidence


def check_evidence(report: dict[str, Any]) -> None:
    records = qg.records_by_id(report["evidence"])
    for check, gate in qg.checks_with_results(report):
        cited = resolve(gate["evidence_ids"], records, f"`{gate['name']}`")
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
            qg.check_ui_status(check, gate, cited)
        elif check["evidence_kind"] == "execution":
            check_execution_status(check, gate, cited)
        else:
            qg.check_observation_status(gate, cited, report["head"])
    for criterion in report["criteria"]:
        where = f"criterion {criterion['number']}"
        cited = resolve(criterion["evidence_ids"], records, where)
        check_criterion_evidence(criterion, cited, report["head"], where)


def resolve(
    evidence_ids: list[str], records: dict[str, dict[str, Any]], where: str
) -> list[dict[str, Any]]:
    for evidence_id in evidence_ids:
        if evidence_id not in records:
            raise InvalidReport(
                "evidence-ids-resolve", f"{where} cites `{evidence_id}`, which no record holds"
            )
    return [records[evidence_id] for evidence_id in evidence_ids]


def check_execution_status(
    check: dict[str, Any], gate: dict[str, Any], cited: list[dict[str, Any]]
) -> None:
    """The "What a result must cite" table for a command check.

    A citation stands in for a finished, stable, passing run here; whether it
    holds is a fact about another file, so currency_problem() decides it.
    """
    status, name = gate["status"], gate["name"]
    runs = [record for record in cited if record["kind"] == "execution"]
    citations = [record for record in cited if record["kind"] == "citation"]
    if status == PASS:
        passing = [r for r in runs if passes(r, check["pass_exit_codes"]) and r["inputs"]["stable"]]
        if not cited or len(passing) + len(citations) != len(cited):
            unstable = any(not record["inputs"]["stable"] for record in runs)
            raise InvalidReport(
                "handoff-unstable-inputs" if unstable else "pass-evidence",
                f"`{name}` is PASS, so every record it cites must finish stable with an exit in "
                f"{check['pass_exit_codes']}",
            )
    elif status == FAIL:
        if not citations and not any(record["state"] == qg.FINISHED for record in runs):
            raise InvalidReport("fail-evidence", f"`{name}` is FAIL and cites no finished record")
    elif status == BLOCKED:
        if not any(
            r["state"] != qg.FINISHED or r["exit_code"] == qg.COMMAND_NOT_FOUND for r in runs
        ):
            raise InvalidReport(
                "blocked-evidence",
                f"`{name}` is BLOCKED and cites no record that stopped or exited 127",
            )


def passes(record: dict[str, Any], pass_exit_codes: list[int]) -> bool:
    return record["state"] == qg.FINISHED and record["exit_code"] in pass_exit_codes


def check_criterion_evidence(
    criterion: dict[str, Any], cited: list[dict[str, Any]], head: str, where: str
) -> None:
    """PASS needs sound evidence and FAIL needs some; the exit code alone never grades.

    A code observation at `head` is enough for a structural criterion, so no
    command has to run only to fill the report.
    """
    verdict = criterion["verdict"]
    if verdict == FAIL and not cited:
        raise InvalidReport("criterion-fail-evidence", f"{where} is FAIL and cites no record")
    if verdict == PASS and (not cited or not all(supports_pass(r, head) for r in cited)):
        raise InvalidReport(
            "criterion-pass-evidence",
            f"{where} is PASS, so it must cite a finished stable run or code read at `head`",
        )


def supports_pass(record: dict[str, Any], head: str) -> bool:
    """A citation's run is checked by currency_problem(), against the file it names."""
    if record["kind"] == "execution":
        return record["state"] == qg.FINISHED and record["inputs"]["stable"]
    if record["kind"] == "observation":
        failed_journey = record["subject"] == "journey" and record["outcome"] != "VERIFY_PASS"
        return record["revision"] == head and not failed_journey
    return True


# ---------------------------------------------------------------- currency


def currency_problem(report: dict[str, Any], facts: CallerFacts) -> Problem | None:
    """The first "current" fact that fails, or None when the report is current.

    The five facts come from the quality-gates validator in its order. Then
    every citation must still hold, since it is a fact about a file this report
    does not own. `report` must already have passed validate() at version 3.
    """
    problem = qg.currency_problem(report, facts)
    if problem is not None:
        return problem
    pass_needs = pass_needs_by_citation(report)
    for record in report["evidence"]:
        if record["kind"] == "citation":
            reason = citation_problem(record, pass_needs.get(record["id"], []), facts.git_dir)
            if reason is not None:
                return Problem("not-current", "citation-holds", f"`{record['id']}` {reason}")
    return None


def pass_needs_by_citation(report: dict[str, Any]) -> dict[str, list[list[int] | None]]:
    """For each citation a PASS result cites, what each such result needs of the cited run.

    A PASS gate needs an exit its check accepts, given as that list of codes. A
    PASS criterion needs only a finished, stable run, given as None.
    """
    needs: dict[str, list[list[int] | None]] = {}
    for check, gate in qg.checks_with_results(report):
        if gate["status"] == PASS and check["pass_exit_codes"] is not None:
            for evidence_id in gate["evidence_ids"]:
                needs.setdefault(evidence_id, []).append(check["pass_exit_codes"])
    for criterion in report["criteria"]:
        if criterion["verdict"] == PASS:
            for evidence_id in criterion["evidence_ids"]:
                needs.setdefault(evidence_id, []).append(None)
    return needs


def citation_problem(
    citation: dict[str, Any], pass_needs: list[list[int] | None], git_dir: Path
) -> str | None:
    """The first of the contract's six citation conditions that fails, or None."""
    path = git_dir / "validation-runs" / citation["run_id"] / "report.json"
    try:
        content = path.read_bytes()
    except OSError:
        return f"names {path}, which cannot be read"
    if hashlib.sha256(content).hexdigest() != citation["report_sha256"]:
        return f"names {path}, which no longer matches `report_sha256`"
    try:
        cited_report = qg.parse_report(content.decode("utf-8", errors="replace"))
        qg.validate(cited_report)
    except InvalidReport as exc:
        return f"names an invalid quality-gates report: {exc}"
    if cited_report["version"] != CURRENT_VERSION or cited_report["run_id"] != citation["run_id"]:
        return "names a report that is not the version 3 run it claims"
    if cited_report["dirty"]:
        return "names a report that checked uncommitted work"
    record = next((r for r in cited_report["evidence"] if r["id"] == citation["evidence_id"]), None)
    if record is None or record["kind"] != "execution":
        return f"names `{citation['evidence_id']}`, which is no execution record of that report"
    if record["inputs"]["digest"] != citation["inputs_digest"]:
        return "measured another inputs digest than the cited record recorded"
    for pass_exit_codes in pass_needs:
        if not (record["state"] == qg.FINISHED and record["inputs"]["stable"]):
            return "supports a PASS, and the cited run did not finish stable"
        if pass_exit_codes is not None and not passes(record, pass_exit_codes):
            return "supports a PASS gate, and the cited run did not exit in its pass codes"
    return None
