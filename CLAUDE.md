# Foxhole

Automated Gmail classification agent using Claude AI + Google Apps Script.

## Project Overview

Foxhole classifies incoming Gmail messages by calling the Claude API (Haiku 4.5) and applying Gmail labels. It runs as a Google Apps Script time-driven trigger (every 2 minutes) — no servers, no local dependencies.

## Architecture

- **Runtime**: Google Apps Script (script.google.com)
- **AI Model**: Claude Haiku 4.5 (`claude-haiku-4-5-20251001`)
- **Gmail access**: Native `GmailApp` + advanced `Gmail` service (for label colors)
- **Trigger**: Time-driven, every 2 minutes
- **Idempotency**: Presence of any `VALID_LABELS` label means "already processed"

## Key File

- `foxhole.js` — The entire agent. Copy/paste into Apps Script editor.

## Label Taxonomy

Labels are applied directly (no prefix). Valid labels:

`urgent`, `action needed`, `follow-up`, `meeting`, `awaiting-reply`, `payment`, `info`, `newsletter`, `marketing`, `notification`, `estimates`, `other`

Each label has a defined color set via the advanced Gmail API in `ensureLabelsExist()`.

## Idempotency

There is no separate "scanned" marker label. An email is considered processed if it already has at least one label from `VALID_LABELS`. The search query dynamically excludes all valid labels. If Claude returns no valid labels, `other` is applied as a fallback — this guarantees every processed email gets at least one label and won't be re-processed.

## Configuration

- `ANTHROPIC_API_KEY` is stored in Apps Script **Script Properties** (not in code).
- `LABEL_PREFIX` is empty string (labels are top-level).
- `MAX_BODY_LENGTH` = 500 chars sent to Claude per email.
- `max_tokens` = 64 (response is a short JSON array).

## Conventions

- This is a single-file Google Apps Script project. Keep it as one file.
- The classification prompt is inline in `callClaude()`. Prompt tuning is the primary way to improve accuracy.
- Error handling: try/catch per thread — failures are skipped and retried on next trigger run.
- Label validation: Claude's response is filtered against `VALID_LABELS` before applying.

## Development Workflow

1. Edit `foxhole.js` locally
2. Run `clasp push` to deploy to Apps Script
3. Test with `testClassification()` (dry run, no labels applied)
4. Test with `classifyNewEmails()` (live, applies labels)
5. Commit changes back to this repo

## Dependencies

- Google Apps Script runtime (GmailApp, UrlFetchApp, PropertiesService)
- Advanced Gmail service (must be enabled in Apps Script for label colors)
- Claude API (Anthropic)

## Agent skills

### Issue tracker

Issues live in this repo's local beads database (`.beads/`), driven by the `bd` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles, each label string equal to its role name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->
