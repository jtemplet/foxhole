#!/usr/bin/env python3
"""Create, claim, finish, and abandon one validation invocation.

An invocation is one run of /tadw:quality-gates or /tadw:verify-acceptance
(docs/validation-report-contract.md, "Invocation identity"). A caller runs this
helper before any grading, and the helper makes four promises:

  * The run has a random UUID, supplied by the caller or created here.
  * Its expected inputs are frozen in `invocation.json` before grading starts.
  * One git directory holds at most one active invocation per report kind. The
    claim is a file, created by a hard link, so two callers cannot both win.
  * The claim stays until its owner finishes it after reading the report, or
    abandons it after the run ended. Nobody else can remove it.

    python3 validation_invocation.py reserve --git-dir D --kind K --bead B
        --owner O [--run-id ID]
    python3 validation_invocation.py prepare --git-dir D --kind K --head H
        --mode M --expected FILE --owner O [--bead B] [--owner-pid N]
        [--run-id ID] [--pending]
    python3 validation_invocation.py finish --git-dir D --kind K --run-id ID
        --owner O
    python3 validation_invocation.py abandon --git-dir D --kind K --run-id ID
        --owner O

`reserve` prints the run ID it reserved. `prepare` prints the descriptor path,
which the skill hands to the report writer as `--invocation`. EXIT CODES: 0 done,
1 refused (stderr says why, and nothing changed), 2 usage error (a malformed or
unreadable argument, or a file the helper cannot read or write, as in the report
writers).

EVERY VALUE IS INJECTED. The caller passes the git directory, HEAD, bead, and
the file holding the expected inputs. This script runs no git and no bd, so its
suite needs no repository (ADR 0011, rule 4). The process check is a parameter
too.

WHAT ENDS A CLAIM. `finish` ends the claim of an invocation whose report was
read. `abandon` ends the claim of an invocation that stopped early, such as an
interrupted cold call. The run directory stays either way, so every completed
report and every cited log remains readable. An ended ID can never be prepared
again, because a retry needs a new ID and a report from the earlier attempt must
not satisfy it.

WHO MAY END A CLAIM. Only the owner that prepared it. An owner is a token the
caller picks, such as a session ID or an attempt ID, and passes on every call.
An owner that also passes `--owner-pid` cannot abandon while that process still
runs. Ending a claim that already ended is a no-op, so a hook that fires twice
is harmless.

WHERE THE CLAIM LIVES. `<git-dir>/validation-runs/active-<kind>.json` holds the
whole descriptor plus the owner, so an adopter compares against the claim and
not against a descriptor that a crashed `prepare` never wrote. Each kind has its
own claim, and each worktree has its own git directory.

WHERE A HOOKED RUN GETS ITS ID. The label hook runs `reserve` when it writes a
pending Stop marker. That records the run ID, the bead, and the hook's owner in
`<git-dir>/validation-runs/pending-<kind>.json`, and the hook writes the same ID
into the marker. The skill runs `prepare --pending`, which adopts that ID and
owner when the reservation is for its bead and its run has not ended. So the
hook and the skill agree on one descriptor through a file, and no process has to
change another's environment. With no live reservation, `prepare --pending`
creates its own ID, as a hook-free or bead-free run does. A second `reserve` for
the same bead and owner adopts the live reservation, so a hook that fires twice
for one run records one ID. An explicit `--run-id` always wins.

Stdlib only, and Python 3.9, since /usr/bin/python3 is what a consumer
repository is guaranteed to have.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

EXIT_DONE = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2

RUNS_DIRECTORY = "validation-runs"
DESCRIPTOR_NAME = "invocation.json"
RESERVATION_PREFIX = "pending-"

QUALITY_GATES = "quality-gates"
ACCEPTANCE = "acceptance"
KINDS = (QUALITY_GATES, ACCEPTANCE)
MODES = ("preparation", "strict")

RUN_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
HEAD_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")

# The keys an `expected` object holds, by kind (contract, "Expected inputs").
EXPECTED_KEYS = {
    QUALITY_GATES: {"checks"},
    ACCEPTANCE: {"checks", "criteria"},
}


class Refused(Exception):  # noqa: N818
    """The request is well formed and the current state forbids it."""


class UsageError(Exception):  # noqa: N818
    """An argument is malformed or unreadable."""


@dataclass(frozen=True)
class Invocation:
    """The fixed facts of one run, as the descriptor records them."""

    run_id: str
    kind: str
    bead: str | None
    head: str
    mode: str
    expected: dict

    def __post_init__(self) -> None:
        _require(RUN_ID_PATTERN.fullmatch(self.run_id), "run id must be a lowercase UUID version 4")
        _require(self.kind in KINDS, f"kind must be one of {', '.join(KINDS)}")
        _require(self.bead != "", "bead must be non-empty, or absent")
        _require(HEAD_PATTERN.fullmatch(self.head), "head must be 40 or 64 lowercase hex digits")
        _require(self.mode in MODES, f"mode must be one of {', '.join(MODES)}")
        _require_expected(self.kind, self.expected)


@dataclass(frozen=True)
class Holder:
    """Who owns a claim: a caller-chosen token, and optionally its process."""

    owner: str
    pid: int | None = None

    def __post_init__(self) -> None:
        _require(self.owner != "", "owner must be non-empty")
        _require(self.pid is None or self.pid > 0, "owner pid must be a positive integer")


@dataclass(frozen=True)
class Reservation:
    """The run a hook set aside for the next invocation of one kind and bead.

    A run ID of None asks `reserve` to adopt a live reservation or create an ID.
    """

    kind: str
    bead: str
    owner: str
    run_id: str | None = None

    def __post_init__(self) -> None:
        _require(self.kind in KINDS, f"kind must be one of {', '.join(KINDS)}")
        _require(bool(self.bead), "a reservation needs a bead")
        _require(bool(self.owner), "owner must be non-empty")
        valid_id = self.run_id is None or RUN_ID_PATTERN.fullmatch(self.run_id)
        _require(valid_id, "run id must be a lowercase UUID version 4")


@dataclass(frozen=True)
class RunRef:
    """Names one invocation of one kind."""

    kind: str
    run_id: str


@dataclass(frozen=True)
class Claim:
    invocation: Invocation
    holder: Holder

    def to_dict(self) -> dict:
        return {**asdict(self.invocation), "owner": self.holder.owner, "owner_pid": self.holder.pid}

    @classmethod
    def from_dict(cls, raw: Any, source: Path) -> Claim:
        try:
            fields = dict(raw)
            owner, pid = fields.pop("owner"), fields.pop("owner_pid")
            return cls(Invocation(**fields), Holder(owner, pid))
        except (TypeError, KeyError, ValueError, UsageError) as exc:
            raise Refused(f"{source} is not a valid claim: {exc}") from exc


class RunStore:
    """The files under `<git-dir>/validation-runs/`."""

    def __init__(self, git_dir: Path) -> None:
        self._root = git_dir / RUNS_DIRECTORY

    def descriptor_path(self, run_id: str) -> Path:
        return self._root / run_id / DESCRIPTOR_NAME

    def read_claim(self, kind: str) -> Claim | None:
        path = self._claim_path(kind)
        raw = _read_json(path)
        return None if raw is None else Claim.from_dict(raw, path)

    def create_claim(self, claim: Claim) -> bool:
        """Create the claim unless one exists. Return whether this call made it."""
        staged = self._stage(claim)
        try:
            return _link_exclusive(staged, self._claim_path(claim.invocation.kind))
        finally:
            staged.unlink()

    def remove_claim(self, kind: str) -> None:
        self._claim_path(kind).unlink()

    def read_descriptor(self, run_id: str) -> dict | None:
        return _read_json(self.descriptor_path(run_id))

    def has_ended(self, run_id: str) -> bool:
        """A run ended when its descriptor exists and it no longer holds its kind's claim."""
        recorded = self.read_descriptor(run_id)
        if recorded is None:
            return False
        held = self.read_claim(recorded.get("kind", ""))
        return held is None or held.invocation.run_id != run_id

    def read_reservation(self, kind: str) -> Reservation | None:
        path = self._reservation_path(kind)
        raw = _read_json(path)
        if raw is None:
            return None
        try:
            return Reservation(**raw)
        except (TypeError, UsageError) as exc:
            raise Refused(f"{path} is not a valid reservation: {exc}") from exc

    def write_reservation(self, reservation: Reservation) -> None:
        _write_json(self._reservation_path(reservation.kind), asdict(reservation))

    def write_descriptor(self, invocation: Invocation) -> Path:
        path = self.descriptor_path(invocation.run_id)
        _write_json(path, asdict(invocation))
        return path

    def _stage(self, claim: Claim) -> Path:
        staged = self._root / f".claim-{uuid.uuid4().hex}"
        _write_json(staged, claim.to_dict())
        return staged

    def _claim_path(self, kind: str) -> Path:
        return self._root / f"active-{kind}.json"

    def _reservation_path(self, kind: str) -> Path:
        return self._root / f"{RESERVATION_PREFIX}{kind}.json"


def reserve(store: RunStore, wanted: Reservation) -> Reservation:
    """Record the run ID a hooked invocation will use, and return the reservation.

    A live reservation for the same bead and owner is adopted, so a hook that
    fires twice for one run records one ID. Otherwise the requested ID, or a new
    one, replaces whatever was reserved before.
    """
    held = store.read_reservation(wanted.kind)
    if held is not None and held.owner == wanted.owner and _continues(store, held, wanted):
        return held
    _refuse_ended_reservation(store, wanted)
    chosen = replace(wanted, run_id=wanted.run_id or str(uuid.uuid4()))
    store.write_reservation(chosen)
    return chosen


def _refuse_ended_reservation(store: RunStore, wanted: Reservation) -> None:
    if wanted.run_id is not None and store.has_ended(wanted.run_id):
        raise Refused(f"invocation {wanted.run_id} already ended; reserve a new run id")


def pending_reservation(store: RunStore, wanted: Reservation) -> Reservation | None:
    """The live reservation a skill run continues, or None when the run needs its own ID.

    The owner of `wanted` is not compared: the skill takes the hook's owner, so
    the hook's Stop handler can finish the claim.
    """
    held = store.read_reservation(wanted.kind)
    return held if held is not None and _continues(store, held, wanted) else None


def _continues(store: RunStore, held: Reservation, wanted: Reservation) -> bool:
    same_run = wanted.run_id in (None, held.run_id)
    return held.bead == wanted.bead and same_run and not store.has_ended(held.run_id)


def prepare(store: RunStore, invocation: Invocation, holder: Holder) -> Path:
    """Claim the kind for this invocation, freeze it, and return the descriptor path.

    Preparing a live ID again, as its own owner, changes nothing.
    """
    claim = Claim(invocation, holder)
    _refuse_changed_inputs(store.read_descriptor(invocation.run_id), invocation)
    held = _acquire(store, claim)
    if held is not None:
        _adopt(held, claim)
    return store.write_descriptor(invocation)


def finish(store: RunStore, ref: RunRef, owner: str) -> str:
    """End the claim after its report was consumed. Return a line saying what happened."""
    claim = _owned_claim(store, ref, owner, "finish")
    return _release(store, ref, claim, "finish")


def abandon(
    store: RunStore,
    ref: RunRef,
    owner: str,
    is_alive: Callable[[int], bool] | None = None,
) -> str:
    """End the claim of an invocation that stopped early. Refuse while its process runs."""
    claim = _owned_claim(store, ref, owner, "abandon")
    _refuse_running(claim, ref, is_alive or pid_is_running)
    return _release(store, ref, claim, "abandon")


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire(store: RunStore, claim: Claim) -> Claim | None:
    """Return None when this call took the claim, else the claim that stands."""
    kind = claim.invocation.kind
    held = store.read_claim(kind)
    if held is None:
        _refuse_ended(store, claim.invocation)
        held = None if store.create_claim(claim) else store.read_claim(kind)
    return held


def _adopt(held: Claim, wanted: Claim) -> None:
    if held.invocation.run_id != wanted.invocation.run_id:
        raise Refused(
            f"{wanted.invocation.kind} is already claimed by invocation {held.invocation.run_id}; "
            "its owner must finish or abandon it first"
        )
    _refuse_changed_inputs(asdict(held.invocation), wanted.invocation)
    if held.holder.owner != wanted.holder.owner:
        raise Refused(f"invocation {held.invocation.run_id} belongs to another owner")


def _owned_claim(store: RunStore, ref: RunRef, owner: str, verb: str) -> Claim | None:
    """Return the live claim of this invocation, or None when it is not the active one."""
    held = store.read_claim(ref.kind)
    if held is None or held.invocation.run_id != ref.run_id:
        return None
    if held.holder.owner != owner:
        raise Refused(f"cannot {verb} invocation {ref.run_id}: it belongs to another owner")
    return held


def _refuse_running(claim: Claim | None, ref: RunRef, is_alive: Callable[[int], bool]) -> None:
    if claim is not None and claim.holder.pid is not None and is_alive(claim.holder.pid):
        raise Refused(
            f"cannot abandon invocation {ref.run_id}: process {claim.holder.pid} still runs"
        )


def _release(store: RunStore, ref: RunRef, claim: Claim | None, verb: str) -> str:
    if claim is None:
        return _already_ended(store, ref, verb)
    store.write_descriptor(claim.invocation)
    store.remove_claim(ref.kind)
    return f"{verb}ed invocation {ref.run_id}"


def _already_ended(store: RunStore, ref: RunRef, verb: str) -> str:
    recorded = store.read_descriptor(ref.run_id)
    if recorded is None or recorded.get("kind") != ref.kind:
        raise Refused(f"cannot {verb} invocation {ref.run_id}: no such {ref.kind} invocation")
    return f"invocation {ref.run_id} had already ended"


def _refuse_ended(store: RunStore, invocation: Invocation) -> None:
    if store.read_descriptor(invocation.run_id) is not None:
        raise Refused(f"invocation {invocation.run_id} already ended; prepare a new run id")


def _refuse_changed_inputs(recorded: dict | None, invocation: Invocation) -> None:
    if recorded is not None and recorded != asdict(invocation):
        raise Refused(f"invocation {invocation.run_id} exists with different fixed inputs")


def _require(condition: object, message: str) -> None:
    if not condition:
        raise UsageError(message)


def _require_expected(kind: str, expected: Any) -> None:
    _require(isinstance(expected, dict), "expected must be a JSON object")
    required = EXPECTED_KEYS[kind]
    _require(set(expected) == required, f"expected for {kind} must hold exactly {sorted(required)}")
    for key in required:
        _require(isinstance(expected[key], list), f"expected.{key} must be a list")


def _link_exclusive(source: Path, target: Path) -> bool:
    """Link `target` to `source` atomically. Return False when `target` exists."""
    try:
        os.link(source, target)
    except FileExistsError:
        return False
    return True


def _read_json(path: Path) -> Any | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise Refused(f"{path} is not valid JSON: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    staged.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    staged.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        print(_run(args))
    except Refused as exc:
        return _report("refused", exc, EXIT_REFUSED)
    except (UsageError, OSError) as exc:
        return _report("error", exc, EXIT_USAGE)
    return EXIT_DONE


def _report(label: str, error: Exception, code: int) -> int:
    print(f"{label}: {error}", file=sys.stderr)
    return code


def _run(args: argparse.Namespace) -> str:
    store = RunStore(args.git_dir)
    if args.command == "reserve":
        return str(
            reserve(store, Reservation(args.kind, args.bead, args.owner, args.run_id)).run_id
        )
    if args.command == "prepare":
        _adopt_pending(store, args)
        return str(prepare(store, _invocation_from(args), Holder(args.owner, args.owner_pid)))
    ref = RunRef(args.kind, args.run_id)
    if args.command == "finish":
        return finish(store, ref, args.owner)
    return abandon(store, ref, args.owner)


def _adopt_pending(store: RunStore, args: argparse.Namespace) -> None:
    """With --pending, take the run ID and owner of a live reservation for this bead."""
    if not args.pending or not args.bead:
        return
    wanted = Reservation(args.kind, args.bead, args.owner, args.run_id)
    held = pending_reservation(store, wanted)
    if held is not None:
        args.run_id, args.owner = held.run_id, held.owner


def _invocation_from(args: argparse.Namespace) -> Invocation:
    return Invocation(
        run_id=args.run_id or str(uuid.uuid4()),
        kind=args.kind,
        bead=args.bead,
        head=args.head.lower(),
        mode=args.mode,
        expected=_load_expected(args.expected),
    )


def _load_expected(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UsageError(f"cannot read the expected inputs from {path}: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    _add_reserve(commands)
    _add_prepare(commands)
    _command(commands, "finish")
    _command(commands, "abandon")
    return parser


def _add_reserve(commands: Any) -> None:
    command = _command(commands, "reserve")
    command.add_argument("--bead", required=True)


def _add_prepare(commands: Any) -> None:
    command = _command(commands, "prepare")
    command.add_argument("--head", required=True)
    command.add_argument("--mode", choices=MODES, required=True)
    command.add_argument("--expected", type=Path, required=True)
    _add_prepare_options(command)


def _add_prepare_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--bead")
    command.add_argument("--owner-pid", type=int)
    command.add_argument("--pending", action="store_true")


def _command(commands: Any, name: str) -> argparse.ArgumentParser:
    command = commands.add_parser(name)
    command.add_argument("--git-dir", type=Path, required=True)
    command.add_argument("--kind", choices=KINDS, required=True)
    command.add_argument("--owner", type=_owner, required=True)
    command.add_argument("--run-id", type=_run_id, required=name in ("finish", "abandon"))
    return command


def _owner(text: str) -> str:
    if not text:
        raise argparse.ArgumentTypeError("owner must be non-empty")
    return text


def _run_id(text: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(text):
        raise argparse.ArgumentTypeError("run id must be a lowercase UUID version 4")
    return text


if __name__ == "__main__":
    sys.exit(main())
