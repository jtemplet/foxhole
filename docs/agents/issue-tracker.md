# Issue tracker: beads (`bd`)

Issues and specs for this repo live in a local beads database under `.beads/`.
Use the `bd` command-line tool for all operations. There is no remote issue
tracker; GitHub Issues on `jtemplet/foxhole` is not used.

The issue prefix is `fox`, so ids look like `fox-83o`. The Dolt database is
named `foxhole`. Run `bd prime` at the start of a session for the full command
reference; this file records the conventions that `bd prime` does not.

## Conventions

- **Create an issue**: `bd create "<title>" -d "<description>" -t <type> -p <priority>`.
  Use `--acceptance`, `--design`, and `--notes` for the structured fields rather
  than stuffing everything into the description. `--body-file -` reads a long
  description from stdin.
- **Read an issue**: `bd show <id>`. Add `--json` when you need to parse it.
- **List issues**: `bd list --status open --json`. Filter with `--label`,
  `--type`, `--priority`, `--assignee`.
- **Find ready work**: `bd ready` (open, no active blockers). `bd blocked` shows
  the rest and why.
- **Search**: `bd search "<text>"`. Run this before creating anything, so a
  duplicate is found first.
- **Comment**: `bd comment <id> "<text>"`. `bd note <id> "<text>"` appends to notes.
- **Apply / remove labels**: `bd label add <id> <label>` / `bd label remove <id> <label>`.
- **Change status**: `bd update <id> --claim` to start work, `bd close <id>` to
  finish, `bd reopen <id>` to undo a close.
- **Dependencies**: `bd dep add <blocked-id> <blocker-id>`, read as "the first
  issue is blocked by the second". `bd dep tree <id>` shows the graph.

Beads auto-discovers the database at `.beads/*.db`. Run every command from
inside the repo, or pass `-C /Users/jtempleton/Dev/foxhole`.

## When a skill says "publish to the issue tracker"

Create a bead with `bd create`.

## When a skill says "fetch the relevant ticket"

Run `bd show <id>`.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a parent bead; **tickets** are its children.

- **Map**: `bd create "<topic> map" -t epic -l wayfinder:map`, holding the
  Notes / Decisions-so-far / Fog body.
- **Child ticket**: `bd create "<title>" --parent <map-id> -l wayfinder:<type>`,
  where `<type>` is `research`, `prototype`, `grilling`, or `task`.
  `bd children <map-id>` lists them.
- **Blocking**: `bd dep add <child-id> <blocker-id>`. A ticket is unblocked when
  every blocker is closed; `bd ready` already applies this filter.
- **Frontier query**: `bd ready --json`, kept to the map's children and to beads
  with no assignee. First in map order wins.
- **Claim**: `bd update <id> --claim`, which atomically sets the assignee to you
  and the status to `in_progress`.
- **Resolve**: `bd comment <id> "<answer>"`, then `bd close <id>`, then append a
  context pointer to the map's Decisions-so-far with `bd note <map-id> "..."`.
