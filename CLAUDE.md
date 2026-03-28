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
