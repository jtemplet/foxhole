#!/usr/bin/env bash
# Claude Code and Codex hook: label the bead a skill invocation acts on.
#
# Wired to four events and dispatches on hook_event_name:
#
#   PreToolUse (matcher Skill)  Claude invoked the Skill tool.
#   PreToolUse (matcher Agent)  Claude started the acceptance-verifier
#                               agent with the Agent tool. Same flow as
#                               the verify-acceptance skill.
#   UserPromptSubmit            A person typed a Claude slash command or
#                               a Codex dollar-prefixed skill name. Same
#                               flow, keyed on the command or skill name.
#   Stop                        Resolves labels that needed an outcome.
#   SubagentStop                Says when the acceptance-verifier agent
#                               finished and wrote no report.
#
# All three entry points converge on run_label_flow, so a skill labels the
# same whichever way it was started.
#
# Three modes, because a PreToolUse hook fires BEFORE the skill runs.
# Measured: the PostToolUse hook for the same call fires ~30ms later, so
# it is equally blind. Only Stop fires after the work.
#
#   apply   The label describes the invocation itself, so it is written
#           immediately. /simplify, /code-review, /tadw:fresh-eyes-cr.
#
#   gate    The label describes an outcome that leaves a readable
#           artifact. /qa writes .gstack/qa-reports/*.md,
#           /quality-gates writes <git-dir>/quality-gates-report.json
#           carrying its verdict verbatim, and /build writes
#           <git-dir>/build-report.json carrying its counts. PreToolUse
#           drops a pending marker naming the skill; Stop reads that
#           skill's report and applies the label only if the report is
#           newer than the marker and clears the gate. Deterministic, no
#           model involvement in the decision.
#
#           The artifact is also a COMPLETION TOKEN. Stop fires whenever
#           Claude yields, not when the work finishes, so an interrupted
#           run reaches Stop looking much like a finished one. Gate mode
#           leaves its marker in place while the artifact is absent and
#           waits across as many Stop events as it takes, so such a run
#           is abandoned at the TTL rather than labeled early.
#
#   inject  The label describes an outcome with no artifact. PreToolUse
#           emits an instruction naming the bead, the gate, and the
#           command, and Claude applies the label at the end. Weaker
#           than gate, and honest about being weaker: measured across
#           this log, inject landed 12 of 31 labels and gate landed
#           2 of 2.
#
#           NO SKILL USES THIS MODE. /verify-acceptance was the last one
#           and moved to gate on 2026-09-09, where it had landed 5 of 15.
#           The code stays for one reason: markers written before that
#           date carry "inject" on their fourth line, and Stop still
#           has to resolve them. Do not add a new inject entry.
#           A verdict that exists only in prose is a verdict the hook
#           cannot check, so give the skill an artifact instead.
#
# Status is separate from labels, and only /build sets it. A label
# records what a run DID and can need the run's verdict first; a status
# records what is HAPPENING and is known at invocation. So /build moves
# an open bead to in_progress immediately, and still defers
# "implemented" to the report Phase 6 writes. It claims under a
# per-session actor, and it REFUSES /build on a bead another session or
# tool already holds, and on an epic or a milestone, which groups other
# beads and holds no work of its own. Those refusals are the only paths
# that stop a skill.
#
# Every failure path logs to stderr and exits 0, so a skill runs whether
# or not the bead could be labeled. Stdout carries only well-formed hook
# JSON: the /build refusal, the /build claim notice, and inject mode.
#
# Because exiting 0 hides an outage, every invocation with a job to do
# also appends its outcome to <git-common-dir>/bead-label.log, and
# `--doctor` answers the same question ahead of time for the current
# branch, writing nothing.

set -uo pipefail

BEADS_FILE=".beads/issues.jsonl"

# The durable log is truncated to its last this-many lines. The hook fires on
# every /simplify and /code-review in a long-lived checkout, so the file has to
# be bounded; a rotation scheme is more machinery than reading back the last
# few hundred outcomes ever needs.
LOG_MAX_LINES=1000

# Deployed copies of this script drift from the source in scripts/, and a log
# line that does not say WHICH copy wrote it cannot tell a stale copy from a
# broken one.
#
# The hash is read from the file at run time rather than stamped in at install
# time. Stamping would make every installed copy differ from its source by
# exactly the line asserting they are the same, which is the drift this exists
# to detect. Reading it costs one subprocess, and only on an invocation that
# already had a job to do.
SCRIPT_PATH="${BASH_SOURCE[0]}"
SCRIPT_HASH=""

# The agent /verify-acceptance sends the grading to. SubagentStop names it in
# agent_type as tadw:acceptance-verifier, and PreToolUse names it the same way
# in tool_input.subagent_type, so it is matched as a suffix.
ACCEPTANCE_AGENT="acceptance-verifier"

# The skill that agent grades for. A marker the agent's start writes names this
# skill, so Stop reads the same report whichever way the grading began.
ACCEPTANCE_SKILL="tadw:verify-acceptance"

# The /build claim decision and the bead candidate list, resolved as a sibling
# of this script. The installer copies it alongside.
CLAIM_MODULE="claim_bead_for_build.py"

# What Stop and SubagentStop do with each pending marker, resolved as a sibling
# of this script. It owns the marker TTL and reads each report through
# REPORTS_MODULE, the rules each report format must meet to earn its label. The
# installer copies both alongside.
STOP_MODULE="resolve_stop_markers.py"
REPORTS_MODULE="bead_label_reports.py"

# The validation invocation helper, resolved as a sibling of this script. A
# quality-gates or acceptance marker records the run ID it reserves, so Stop
# consumes the report of that run alone. Without it the marker is a legacy one.
INVOCATION_MODULE="validation_invocation.py"

log() { echo "[bead-label] $*" >&2; }
quiet_exit() { exit 0; }

# Twelve hex characters of the script's own sha256. Cached, since handle_stop
# can log several outcomes in one run. shasum is the macOS spelling and
# sha256sum the GNU one; if neither is there the log says "unknown" rather than
# losing the line.
script_hash() {
  if [[ -z "$SCRIPT_HASH" ]]; then
    SCRIPT_HASH="$( { shasum -a 256 "$SCRIPT_PATH" 2>/dev/null || sha256sum "$SCRIPT_PATH" 2>/dev/null; } | cut -c1-12)"
    [[ -n "$SCRIPT_HASH" ]] || SCRIPT_HASH="unknown"
  fi
  echo "$SCRIPT_HASH"
}

# ---------------------------------------------------------------------
# The durable log
# ---------------------------------------------------------------------
#
# Every failure path here exits 0 by design, so a skill runs whether or not its
# bead could be labeled. That is right, and it is also why a total outage is
# invisible: the only record was stderr, which nothing surfaces in normal use.
# It has now hidden one twice. Between the 2026-08-12 tracker cutover and the
# fix for it, every label attempt logged a failure nobody read and no bead was
# labeled. Then a full build-and-ship session on 2026-08-22 ran three labeled
# skills against an unresolvable branch and shipped the bead unlabeled, found
# afterwards by inspection rather than by anything in the session.
#
# So the outcome also goes somewhere a person can read later. Exiting 0 is
# unchanged; this adds visibility, not a failure mode. One tab-separated line
# per invocation that got as far as HAVING a job to do: timestamp, event,
# skill, branch, resolved id or "unresolved", action, and the hash of the copy
# of this script that wrote it. An unmapped skill writes nothing, since a hook
# correctly declining to label /adr is not an outcome and logging it would bury
# the ones that are.
#
# log_outcome <event> <skill> <branch> <id-or-empty> <action>
log_outcome() {
  [[ -n "${GIT_COMMON:-}" ]] || return 0
  local file="$GIT_COMMON/bead-label.log"
  printf '%s\t%s\t%s\t%s\t%s\t%s\tscript=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "${2:--}" "${3:--}" "${4:-unresolved}" "$5" \
    "$(script_hash)" \
    >> "$file" 2>/dev/null || return 0
  trim_log "$file"
}

trim_log() {
  local file="$1" lines trimmed
  lines="$(wc -l < "$file" 2>/dev/null || echo 0)"
  (( lines > LOG_MAX_LINES )) || return 0
  trimmed="$file.trimming"
  # Written whole and then moved, so a run interrupted mid-trim leaves the log
  # intact rather than half a file.
  if tail -n "$LOG_MAX_LINES" "$file" > "$trimmed" 2>/dev/null; then
    mv "$trimmed" "$file" 2>/dev/null || rm -f "$trimmed" 2>/dev/null
  else
    rm -f "$trimmed" 2>/dev/null
  fi
}

# ---------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------

# Sets REPO_ROOT, MAIN_ROOT, MARKER_DIR. Exits when
# not usable.
init_repo() {
  REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || quiet_exit
  cd "$REPO_ROOT" || quiet_exit

  GIT_COMMON="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
  MAIN_ROOT="$(dirname "$GIT_COMMON")"

  # bd is the only tracker this hook supports. It takes no --db pin: it resolves
  # one workspace per repository through the git common dir, so it finds the
  # same database from a worktree as from the main checkout.
  local beads_dir="$MAIN_ROOT/.beads"
  [[ -d "$beads_dir" ]] || { log "no beads workspace under $beads_dir"; quiet_exit; }
  local t
  for t in bd jq git; do
    command -v "$t" >/dev/null 2>&1 || { log "$t not on PATH"; quiet_exit; }
  done

  # Markers live inside the git common dir: never tracked, never dirty
  # the tree, and shared across worktrees.
  MARKER_DIR="$GIT_COMMON/pending-bead-labels"
}

has_label() {
  local json="$1" label="$2"
  echo "$json" | jq -e --arg l "$label" \
    'if type == "array" then .[0] else . end | (.labels // []) | index($l)' >/dev/null 2>&1
}

add_label() {
  local bead_id="$1" label="$2"
  bd update "$bead_id" --add-label "$label" >/dev/null 2>&1 || {
    log "bd update $bead_id --add-label $label failed"
    return 1
  }
  log "labeled $bead_id $label"
  refresh_export
}

# Move the bead to in_progress as the work starts, and echo what happened.
#
# Deliberately not a label, and deliberately not deferred. A label records what
# a run DID, so /build's "implemented" has to wait for the run's own verdict.
# A status records what is HAPPENING, and that is known at invocation. Deferring
# it would leave the bead reading `open` for the whole run, so `bd ready` would
# go on offering work already underway.
#
# The decision, the two bd writes, and the hook JSON live in claim_bead_for_build.py
# beside this script, where a unit test can call the decision directly. Status,
# assignee and issue type come from RESOLVED_JSON, which resolve_bead already
# fetched, so this costs no extra `bd show`.
#
# Echoes the module's JSON, {"outcome": ..., "hook_output": ...}, and returns
# its exit status: 0 when this session holds the bead or its status was left
# alone, 1 when the claim failed, and 2 when /build must not run: another
# holder has the bead, or it is a container. A missing python3 or module is a
# failed claim, never a refusal.
claim_bead() {
  local bead_id="$1" module
  module="$(dirname "$SCRIPT_PATH")/$CLAIM_MODULE"
  if ! command -v python3 >/dev/null 2>&1 || [[ ! -f "$module" ]]; then
    log "python3 or $CLAIM_MODULE is missing; /build runs without a claim"
    jq -cn --arg m "$CLAIM_MODULE" '{outcome: "FAILED, python3 or \($m) is missing", hook_output: null}'
    return 1
  fi
  printf '%s' "${payload:-}" | python3 "$module" claim "$bead_id" \
    "$(bead_field "$RESOLVED_JSON" status)" "$(bead_field "$RESOLVED_JSON" assignee)" \
    "$(bead_field "$RESOLVED_JSON" issue_type)"
}

# The session the hook payload names, or nothing. `payload` is the global the
# dispatch at the foot of this file reads from stdin.
payload_session() {
  echo "${payload:-}" | jq -r '.session_id // empty' 2>/dev/null || true
}

bead_field() {  # bead_field <json> <field>
  echo "$1" | jq -r --arg f "$2" 'if type == "array" then .[0] else . end | .[$f] // ""' 2>/dev/null || true
}

# Leave the working tree exactly as clean as it was found.
#
# .beads/issues.jsonl is a passive export bd never refreshes on its own, and
# committing it would carry a stale file into the diff. So this used to refresh
# it after every label. That cost landed on two other tools, both of which
# refuse to run on a dirty tree: outrigger aborts its pre-flight with "tracked
# files are modified (uncommitted changes)", and /tadw:ship Step 4 found the
# file already modified before its squash-merge and had to back it up and
# discard it. Both fired in the 2026-08-22 fathom session while this hook was
# labeling nothing at all; apply mode runs on every /simplify, /code-review and
# /tadw:fresh-eyes-cr, so a working hook would have collided on every review
# pass.
#
# The default flipped rather than the feature going away. Refreshing an export
# that is ALREADY modified dirties nothing further, so that case still runs.
# bv reads the bd database directly and loses nothing either way. Manifest's
# reader is unconfirmed, and TADW_BEAD_LABEL_EXPORT=1 restores the old behavior
# for it, or for anyone who genuinely needs the file fresh mid-session.
refresh_export() {
  if [[ "${TADW_BEAD_LABEL_EXPORT:-}" != "1" ]] \
    && [[ -z "$(git status --porcelain -- "$BEADS_FILE" 2>/dev/null)" ]]; then
    log "left $BEADS_FILE alone to keep the tree clean (TADW_BEAD_LABEL_EXPORT=1 to refresh it)"
    return 0
  fi
  bd export -o "$BEADS_FILE" >/dev/null 2>&1 \
    || log "export refresh failed; bv and Manifest will lag"
}

# ---------------------------------------------------------------------
# Bead resolution
# ---------------------------------------------------------------------

# Echo the id prefixes this repository uses, as a regex alternation, or nothing
# when they cannot be determined.
#
# Two sources, both local files, because the whole point is to decide without a
# tracker call: the configured `issue-prefix` in .beads/config.yaml, and the
# leading token of every id already in the tracked export. The export supplies
# the HISTORICAL prefixes the config no longer names, which fathom needs: issues
# created before its 2026-08-12 cutover carry `life-os-` and new ones carry
# `fathom-`.
#
# Echoing nothing DISABLES the filter rather than rejecting everything, so a
# repository with no export and no configured prefix behaves exactly as it did
# before this existed.
#
# Read from MAIN_ROOT, not the working directory: a worktree checks out tracked
# files only, so its own .beads/ carries neither file.
known_id_prefixes() {
  {
    sed -n 's/^issue-prefix:[[:space:]]*//p' "$MAIN_ROOT/.beads/config.yaml" 2>/dev/null
    [[ -f "$MAIN_ROOT/$BEADS_FILE" ]] &&
      sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([a-z][a-z0-9]*\)-.*/\1/p' "$MAIN_ROOT/$BEADS_FILE" 2>/dev/null
  } | tr -d '"' | grep -E '^[a-z][a-z0-9]*$' | sort -u | paste -sd'|' -
}

# Bead ids here range from a bare three-character suffix to a full slug,
# so shape alone cannot tell an id from an ordinary word. Every candidate
# is verified against the tracker: the module narrows the search, and bd
# decides.
#
# The candidate list comes from bead_candidates in claim_bead_for_build.py,
# which owns the token pattern, the ordering, both filters and the probe
# cap. It is handed everything it needs and runs no command, so this
# function is the only place a candidate costs a tracker call. Three things
# stay here because they reach outside the process: the pull request
# lookup, the id prefixes read from local files, and the bd show probes.
#
# Each surviving candidate costs a bd show subprocess, and this hook BLOCKS
# UserPromptSubmit. Measured against fathom's tracker on 2026-08-23, one
# bd show takes 0.53s.
#
# A missing python3 or module offers no candidates, and neither does a module
# that fails the `candidates` call, so nothing is labeled and the skill still
# runs. Both set RESOLVE_FAILURE to the cause, so a caller can tell a broken
# install from a branch that names no bead. It stays empty when the module
# answered and no candidate matched.
#
# $3 is prose that can name the bead, such as the prompt an agent was started
# with. It is searched for ids and never for a pull request: a pull request is
# named by a bare number, and prose is full of those.
resolve_bead() {
  local args="$1" branch="$2" prose="${3:-}" text pr_id pr_json module candidates candidates_code candidate found id
  RESOLVE_FAILURE=""
  text="$args"
  [[ -n "$prose" ]] && text+=$'\n'"$prose"

  pr_id="$(echo "$args" | tr ' ' '\n' | awk '
    /^#?[0-9]+$/      { gsub(/#/, ""); print; exit }
    /github\.com.*pull\/[0-9]+/ {
      sub(/.*\/pull\//, ""); sub(/[^0-9].*/, ""); print; exit
    }
  ')"
  if [[ -n "$pr_id" ]] && command -v gh >/dev/null 2>&1; then
    pr_json="$(gh pr view "$pr_id" --json title,headRefName,body 2>/dev/null || true)"
    if [[ -n "$pr_json" ]]; then
      text+=$'\n'"$(echo "$pr_json" | jq -r '.title, (.headRefName | gsub("/"; " ")), (.body // "")' 2>/dev/null || true)"
    fi
  fi

  module="$(dirname "$SCRIPT_PATH")/$CLAIM_MODULE"
  if ! command -v python3 >/dev/null 2>&1 || [[ ! -f "$module" ]]; then
    RESOLVE_FAILURE="python3 or $CLAIM_MODULE is missing"
    log "$RESOLVE_FAILURE; no bead candidates to probe"
    return 1
  fi
  candidates="$(printf '%s' "$text" | python3 "$module" candidates "$branch" "$(known_id_prefixes)")"
  candidates_code=$?
  # A module older than the subcommand prints its usage and exits nonzero.
  if (( candidates_code != 0 )); then
    RESOLVE_FAILURE="$CLAIM_MODULE candidates exited $candidates_code; the installed copy can predate the candidates subcommand, so run the installer again"
    log "$RESOLVE_FAILURE"
    return 1
  fi
  [[ -z "$candidates" ]] && return 1

  while IFS= read -r candidate; do
    [[ -z "$candidate" ]] && continue
    found="$(bd show "$candidate" --json 2>/dev/null || true)"
    [[ -z "$found" ]] && continue
    id="$(echo "$found" | jq -r 'if type == "array" then .[0] else . end | .id // empty' 2>/dev/null || true)"
    if [[ -n "$id" ]]; then
      RESOLVED_ID="$id"
      RESOLVED_JSON="$found"
      return 0
    fi
  done <<< "$candidates"
  return 1
}

# ---------------------------------------------------------------------
# PreToolUse
# ---------------------------------------------------------------------

# Sets LABEL, MODE and GATE for a skill name. Returns 1 when unmapped.
#
# These are SKILL names, never command names. The PreToolUse payload field
# is tool_input.skill, so a skill reaches this map under the name it was
# resolved to. Where the command name differs it never matches here:
# /tadw:fresh-eyes-cr invokes tadw:review-fresh-eyes, and /tadw:code-review
# dispatches through the code-reviewer agent to a per-language review
# skill. Before adding an entry, confirm the name against the plugin's
# skills/ directory rather than its commands/ directory. To map a command
# name, use skill_for_command below instead.
#
# Both the plugin-qualified and bare forms, since the payload may carry
# either depending on how the skill was resolved.
classify_skill() {
  local skill="$1"
  GATE=""
  CLAIM=""
  case "$skill" in
    # Ordered the way the work moves: implemented, simplified, reviewed, qa-d, accepted.
    #
    # "implemented" is an outcome, not an invocation: /build stops at Ground
    # when the spec is too thin, and a label applied up front would call that
    # run implemented. So it waits for <git-dir>/build-report.json, which
    # Phase 6 writes and bead_label_reports.py reads.
    #
    # This was inject mode until 2026-09-08, and it landed on 41% of runs
    # (7 applied, 10 owed). Two properties of Stop made that unfixable in
    # place: Stop fires whenever Claude yields rather than at completion
    # (measured start-to-first-Stop gaps of 1s, 35s and 8m), and inject mode
    # consumes its marker at that first Stop. Gate mode leaves the marker
    # alone while its artifact is absent, so an interrupted run is abandoned
    # at the TTL instead of being labeled early.
    feature-development|tadw:feature-development)
      LABEL="implemented"; MODE="gate"; CLAIM=1 ;;
    simplify|tadw:code-simplify)
      LABEL="simplified"; MODE="apply" ;;
    # Three entry points earn "reviewed", and only these three: /code-review,
    # /tadw:fresh-eyes-cr, and the mattpocock-skills:code-review skill. Each is
    # a review pass over finished code.
    #
    # The language style skills (style-swift, style-frontend, style-go,
    # terraform-iac-expert, agentic-clean-code) and the per-language review
    # skills were listed here once, to catch /tadw:code-review dispatching
    # through the code-reviewer agent. That over-matched: /build Phase 2 loads
    # the same style skill as the guide to WRITE by, so a plain build labeled
    # its own unreviewed code "reviewed". A label nobody earned is worse than
    # a label a command forgot to apply, so the indirect path is gone. If
    # /tadw:code-review should label again, map it in skill_for_command below,
    # where a command name belongs.
    #
    # mattpocock-skills:code-review is named whole, never as *:code-review. A
    # pattern would label for any plugin's skill of that name, read or not.
    code-review|code-review:code-review|\
    mattpocock-skills:code-review|\
    review-fresh-eyes|tadw:review-fresh-eyes)
      LABEL="reviewed";   MODE="apply" ;;
    # Both leave an artifact Stop can read, one each; the marker records
    # which skill ran, and Stop picks the reader from that.
    qa|gstack:qa|quality-gates|tadw:quality-gates)
      LABEL="qa-d";       MODE="gate" ;;
    # "accepted" is the verdict pipeline B ends on, and the acceptance-verifier
    # agent that produces it is pinned to sonnet by ADR 0008 precisely because
    # nobody downstream re-checks a grade. So the grade has to reach the bead.
    #
    # This was inject mode until 2026-09-09, and it landed on 5 of 15 decided
    # runs. It failed the same way "implemented" did, for the same two reasons:
    # Stop fires whenever Claude yields rather than at completion, and inject
    # mode consumes its marker at that first yield. It waits for
    # <git-dir>/acceptance-report.json, which the agent writes and
    # bead_label_reports.py reads.
    verify-acceptance|tadw:verify-acceptance)
      LABEL="accepted";   MODE="gate" ;;
    *) return 1 ;;
  esac
  return 0
}

# The shared flow, once a skill name is known. $3 names the hook event so
# inject mode can label its own output correctly. $4 is prose that can name the
# bead, which resolve_bead searches for ids alone.
run_label_flow() {
  local skill="$1" args="$2" event="$3" prose="${4:-}" branch claim_result claim_code claim_outcome claim_hook

  init_repo
  branch="$(git branch --show-current 2>/dev/null || true)"

  resolve_bead "$args" "$branch" "$prose" || {
    if [[ -n "$RESOLVE_FAILURE" ]]; then
      log_outcome "$event" "$skill" "$branch" "" "FAILED, $RESOLVE_FAILURE; wanted $LABEL"
      quiet_exit
    fi
    log "no candidate resolved to a bead (branch '$branch')"
    log_outcome "$event" "$skill" "$branch" "" "wanted $LABEL, no candidate resolved to a bead"
    quiet_exit
  }

  # Ahead of the label short-circuit below on purpose. A bead that already
  # carries the label can still have been reopened, and a re-run must move it
  # back to in_progress rather than return early over the top of it.
  #
  # A refusal exits before the marker is written, so a run that never started
  # leaves no pending label behind. That covers a container too: the module
  # refuses it before either bd write.
  if [[ -n "${CLAIM:-}" ]]; then
    claim_result="$(claim_bead "$RESOLVED_ID")"
    claim_code=$?
    claim_outcome="$(jq -r '.outcome // empty' <<< "$claim_result" 2>/dev/null || true)"
    claim_hook="$(jq -c '.hook_output // empty' <<< "$claim_result" 2>/dev/null || true)"
    [[ "$claim_outcome" == claimed* ]] && refresh_export
    log_outcome "$event" "$skill" "$branch" "$RESOLVED_ID" "${claim_outcome:-claim produced no outcome}"
    # python3 itself exits 2 when it cannot open the module, so only an
    # outcome the module wrote counts as a refusal.
    if (( claim_code == 2 )) && [[ "$claim_outcome" == REFUSED* ]]; then
      [[ -n "$claim_hook" ]] && echo "$claim_hook"
      quiet_exit
    fi
    # Inject mode writes its own hook JSON to stdout, and two objects there is
    # malformed output. No claiming skill uses inject today.
    if [[ -n "$claim_hook" && "$MODE" != "inject" ]]; then
      echo "$claim_hook"
    fi
  fi

  if has_label "$RESOLVED_JSON" "$LABEL"; then
    log "$RESOLVED_ID already labeled $LABEL"
    log_outcome "$event" "$skill" "$branch" "$RESOLVED_ID" "already labeled $LABEL"
    quiet_exit
  fi

  case "$MODE" in
    apply)
      if add_label "$RESOLVED_ID" "$LABEL"; then
        log_outcome "$event" "$skill" "$branch" "$RESOLVED_ID" "applied $LABEL"
      else
        log_outcome "$event" "$skill" "$branch" "$RESOLVED_ID" "FAILED to apply $LABEL"
      fi
      ;;
    gate)
      mkdir -p "$MARKER_DIR" 2>/dev/null || {
        log_outcome "$event" "$skill" "$branch" "$RESOLVED_ID" "FAILED to write a $LABEL marker"
        quiet_exit
      }
      # The fifth line names the session that started the run. Markers are
      # shared by every worktree and session, and handle_subagent_stop must
      # clear only the marker of the run whose agent just finished. A
      # validation run adds three lines: its kind, its run ID, and its git dir.
      reserve_invocation "$skill" "$args" "$prose"
      printf '%s\n%s\n%s\ngate\n%s\n%s' "$(date +%s)" "$RESOLVED_ID" "$skill" "$(payload_session)" \
        "$INVOCATION_LINES" > "$MARKER_DIR/${LABEL}__${RESOLVED_ID}"
      log "pending $LABEL for $RESOLVED_ID; Stop will check the run's report"
      log_outcome "$event" "$skill" "$branch" "$RESOLVED_ID" "pending $LABEL, Stop reads the report${INVOCATION_NOTE}"
      ;;
    inject)
      # A marker beside gate's, and its fourth line is what tells them apart.
      # Stop cannot DECIDE an inject label, because inject mode exists exactly
      # where there is no artifact to read. What it can do is say afterwards
      # whether the label the run was asked for ever appeared. Without this a
      # dropped inject label leaves no trace at all: the instruction went to
      # Claude, Claude did not act on it, and nothing recorded a debt.
      if mkdir -p "$MARKER_DIR" 2>/dev/null; then
        printf '%s\n%s\n%s\ninject\n' "$(date +%s)" "$RESOLVED_ID" "$skill" \
          > "$MARKER_DIR/${LABEL}__${RESOLVED_ID}"
      else
        log "could not record that $LABEL was asked for; Stop will not miss it"
      fi
      # The command in this string is for someone to run later. bd resolves its
      # own workspace, so it is one bare word with no path to quote.
      jq -n --arg event "$event" --arg ctx "When this /${skill} run is complete, add the \`${LABEL}\` label to bead ${RESOLVED_ID}, but ONLY if ${GATE}. If it does not clear that gate, add no label and say so. The command is: bd update ${RESOLVED_ID} --add-label ${LABEL}" \
        '{hookSpecificOutput:{hookEventName:$event,additionalContext:$ctx}}'
      log "deferred $LABEL for $RESOLVED_ID to the run's verdict"
      log_outcome "$event" "$skill" "$branch" "$RESOLVED_ID" "deferred $LABEL to the run's verdict"
      ;;
  esac
}

# The report kind a validation skill writes, or nothing for any other skill.
validation_kind() {
  case "$1" in
    quality-gates|tadw:quality-gates)         echo "quality-gates" ;;
    verify-acceptance|tadw:verify-acceptance) echo "acceptance" ;;
  esac
}

# The run ID a caller passed as `--run-id <id>`, in the skill's arguments or in
# the prose an agent was started with. Outrigger passes one per cold attempt.
requested_run_id() {
  local run_id_re='--run-id[=[:space:]]+([0-9A-Za-z-]+)'
  [[ "$1" =~ $run_id_re ]] && echo "${BASH_REMATCH[1]}"
}

# Sets INVOCATION_LINES to the marker lines that tie a validation run to one
# invocation: the kind, the run ID, and the git dir. The helper reserves the ID
# in <git-dir>/validation-runs/pending-<kind>.json, where the skill's
# `prepare --pending` finds it, so no process changes another's environment.
#
# Leaves them empty for any other skill, and when the helper is missing or
# refuses. That marker is a legacy one, which Stop resolves as before, and
# INVOCATION_NOTE says why in the log line.
reserve_invocation() {  # reserve_invocation <skill> <args> <prose>
  local kind git_dir run_id session owner module reserved
  INVOCATION_LINES=""
  INVOCATION_NOTE=""
  kind="$(validation_kind "$1")"
  [[ -n "$kind" ]] || return 0
  module="$(dirname "$SCRIPT_PATH")/$INVOCATION_MODULE"
  git_dir="$(git rev-parse --path-format=absolute --git-dir 2>/dev/null || true)"
  if ! command -v python3 >/dev/null 2>&1 || [[ ! -f "$module" || -z "$git_dir" ]]; then
    INVOCATION_NOTE=", with no run ID because python3, $INVOCATION_MODULE, or the git dir is missing"
    return 0
  fi
  run_id="$(requested_run_id "$2")"
  [[ -n "$run_id" ]] || run_id="$(requested_run_id "$3")"
  session="$(payload_session)"
  owner="bead-label-hook${session:+ session $session}"
  if reserved="$(python3 "$module" reserve --git-dir "$git_dir" --kind "$kind" \
      --bead "$RESOLVED_ID" --owner "$owner" ${run_id:+--run-id "$run_id"} 2>&1)" \
      && [[ "$reserved" =~ ^[0-9a-f-]{36}$ ]]; then
    INVOCATION_LINES="$kind"$'\n'"$reserved"$'\n'"$git_dir"$'\n'
    INVOCATION_NOTE=" of run $reserved"
  else
    log "could not reserve a $kind run ID: $reserved"
    INVOCATION_NOTE=", with no run ID because the reservation failed"
  fi
}

handle_pre() {
  local payload="$1" skill args

  skill="$(echo "$payload" | jq -r '.tool_input.skill // empty' 2>/dev/null || true)"
  if [[ -z "$skill" ]]; then
    handle_agent_start "$payload"
    quiet_exit
  fi
  classify_skill "$skill" || quiet_exit

  args="$(echo "$payload" | jq -r '.tool_input.args // ""' 2>/dev/null || true)"
  run_label_flow "$skill" "$args" PreToolUse
}

# A PreToolUse payload that names no skill: the Agent tool, or nothing we label.
#
# A session can start the acceptance-verifier with the Agent tool directly, with
# no Skill call and no typed command. On 2026-10-11 a grading of atlas-5fum
# returned ACCEPTED that way. No marker had been written, so Stop had nothing to
# resolve, and the label was applied by hand.
#
# Measured on Claude Code 2.1.296: tool_name is "Agent", and tool_input carries
# description, prompt and subagent_type. This reads subagent_type and never
# tool_name, so the tool's older name, Task, is read the same way.
#
# The usual path fires twice: the Skill call writes the marker, then the agent's
# start writes it again. The second write is the same file with a later time,
# and the report is written after both.
handle_agent_start() {
  local payload="$1" agent_type prompt

  agent_type="$(echo "$payload" | jq -r '.tool_input.subagent_type // empty' 2>/dev/null || true)"
  [[ "$agent_type" == *"$ACCEPTANCE_AGENT" ]] || quiet_exit
  classify_skill "$ACCEPTANCE_SKILL" || quiet_exit

  prompt="$(echo "$payload" | jq -r '.tool_input.prompt // ""' 2>/dev/null || true)"
  run_label_flow "$ACCEPTANCE_SKILL" "" PreToolUse "$prompt"
}

# ---------------------------------------------------------------------
# UserPromptSubmit
# ---------------------------------------------------------------------

# Maps a command or explicit Codex skill name to the skill it invokes,
# printing the skill name. Returns 1 when the name is not one we label.
#
# This map exists because PreToolUse cannot see a typed slash command. It
# fires on the Skill TOOL, and typing /foo calls no tool, so without this
# the run finishes unlabeled. Worse for quality-gates and
# verify-acceptance, whose command files tell the reader to open SKILL.md
# directly rather than invoke the skill by name, making the documented
# path the unlabeled one.
#
# Keep this keyed on COMMAND names, the opposite of classify_skill. Where
# the two differ, only the mapping below is correct.
#
# `ship` and `tadw:ship` are deliberately absent, and this is the record of
# that decision rather than an oversight. /tadw:ship CLOSES the bead, so a
# label applied at the same moment carries no information the closed state does
# not already carry. Adding an entry here would buy a label nobody reads and a
# marker Stop then has to resolve.
skill_for_command() {
  case "$1" in
    # /build is the documented entry point (commands/build.md invokes the
    # skill); the bare skill name also resolves, since no command shadows it.
    build|tadw:build|\
    feature-development|tadw:feature-development) echo "tadw:feature-development" ;;
    simplify|tadw:code-simplify)              echo "tadw:code-simplify" ;;
    # /tadw:code-review belongs here rather than in classify_skill, because it is
    # a command name and that map takes skill names only. It dispatches through
    # the code-reviewer agent to a per-language review skill, so there is no one
    # skill to name; every path it takes is a review pass, so it resolves to the
    # same skill the other code-review spellings do and earns the same label.
    code-review|code-review:code-review|\
    tadw:code-review)                         echo "code-review:code-review" ;;
    fresh-eyes-cr|tadw:fresh-eyes-cr|\
    review-fresh-eyes|tadw:review-fresh-eyes) echo "tadw:review-fresh-eyes" ;;
    qa|gstack:qa)                             echo "gstack:qa" ;;
    quality-gates|tadw:quality-gates)         echo "tadw:quality-gates" ;;
    verify-acceptance|tadw:verify-acceptance) echo "tadw:verify-acceptance" ;;
    *) return 1 ;;
  esac
  return 0
}

handle_prompt() {
  local payload="$1" prompt command skill args prompt_re

  prompt="$(echo "$payload" | jq -r '.prompt // empty' 2>/dev/null || true)"
  [[ -z "$prompt" ]] && quiet_exit

  # Only a prompt that STARTS with the command or skill name counts. A
  # prompt merely mentioning /qa or $qa is talking about it, not running it.
  #
  # The pattern is held in a variable rather than written inline, and that is
  # not a style choice. Inline, the `$` has to be escaped from bash, and
  # `[/\$]` inside a bracket expression is the THREE-character set {/, \, $},
  # because POSIX gives a backslash no special meaning in there. A prompt
  # opening with a backslash-escaped word, which a paste of escaped shell text
  # produces, matched: `\build tadw-1` resolved to tadw:feature-development,
  # claimed the bead, and dropped a gate marker for a run that never happened.
  # In a variable the `$` needs no escape, so the class says what it means.
  prompt_re='^[[:space:]]*[/$]([A-Za-z0-9_:-]+)[[:space:]]*(.*)$'
  [[ "$prompt" =~ $prompt_re ]] || quiet_exit
  command="${BASH_REMATCH[1]}"
  args="${BASH_REMATCH[2]}"

  skill="$(skill_for_command "$command")" || quiet_exit
  classify_skill "$skill" || quiet_exit

  run_label_flow "$skill" "$args" UserPromptSubmit
}

# ---------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------

# Says whether an inject label ever landed. It applies nothing: the run was
# asked to, and either did or did not. Reading the bead is the only way to know,
# since inject mode has no artifact by definition.
resolve_inject_marker() {
  local bead_id="$1" label="$2" skill="$3" branch="$4" found
  found="$(bd show "$bead_id" --json 2>/dev/null || true)"

  # An empty read is not the same as a missing label. bd writes its errors to
  # stderr and leaves stdout empty, so calling that "owed" would report a debt
  # that may not exist.
  if [[ -z "$found" ]]; then
    log "could not read $bead_id to confirm $label"
    log_outcome Stop "$skill" "$branch" "$bead_id" "could not confirm $label, $bead_id would not read"
    return 0
  fi

  if has_label "$found" "$label"; then
    log "$bead_id carries $label; the run applied it"
    log_outcome Stop "$skill" "$branch" "$bead_id" "confirmed $label, the run applied it"
  else
    log "$bead_id was owed $label and does not carry it"
    log_outcome Stop "$skill" "$branch" "$bead_id" "OWED $label, the run never applied it"
  fi
}

# Runs the Stop module beside this script and acts on each marker decision it
# prints. The module decides every marker: which report it waits for, whether
# that report is newer than the marker, whose bead it names, whether it passes,
# and when a marker is past its TTL or unreadable. This function makes the
# tracker writes, logs the outcomes, and removes the markers it is told to.
#
# Each record is eleven NUL-terminated fields, since a marker can hold any
# text. The module decides a malformed marker on its own, so the markers behind
# it still resolve. The last three name the invocation claim a consumed run
# report ends, and are empty for every other record.
#
# act_on_marker_decisions <event> <branch> <module arguments...>
act_on_marker_decisions() {
  local event="$1" branch="$2" git_dir="$5" module action marker label bead skill remove
  local message outcome claim_kind claim_run claim_owner
  shift 2
  module="$(dirname "$SCRIPT_PATH")/$STOP_MODULE"
  while IFS= read -r -d '' action && IFS= read -r -d '' marker \
    && IFS= read -r -d '' label && IFS= read -r -d '' bead \
    && IFS= read -r -d '' skill && IFS= read -r -d '' remove \
    && IFS= read -r -d '' message && IFS= read -r -d '' outcome \
    && IFS= read -r -d '' claim_kind && IFS= read -r -d '' claim_run \
    && IFS= read -r -d '' claim_owner; do
    [[ -n "$message" ]] && log "$message"
    case "$action" in
      label)
        add_label "$bead" "$label" || outcome="FAILED to apply $label" ;;
      confirm)
        resolve_inject_marker "$bead" "$label" "$skill" "$branch"
        outcome="" ;;
    esac
    [[ -n "$outcome" ]] && log_outcome "$event" "$skill" "$branch" "$bead" "$outcome"
    [[ "$remove" == "1" ]] && rm -f "$marker"
    if [[ "$remove" == "1" && -n "$claim_run" ]]; then
      finish_invocation "$git_dir" "$claim_kind" "$claim_run" "$claim_owner" \
        || log_outcome "$event" "$skill" "$branch" "$bead" "FAILED to finish $claim_kind run $claim_run"
    fi
  done < <(python3 "$module" "$@")
}

# Ends the claim of a run whose report Stop just consumed, so the next run of
# that kind can claim it. Only the owner that reserved the run may end it; a
# refusal is logged, and the claim stays for its owner to finish or abandon.
finish_invocation() {  # finish_invocation <git-dir> <kind> <run-id> <owner>
  local module result
  module="$(dirname "$SCRIPT_PATH")/$INVOCATION_MODULE"
  if [[ ! -f "$module" ]]; then
    log "$INVOCATION_MODULE is missing; $2 run $3 keeps its claim"
    return 1
  fi
  if ! result="$(python3 "$module" finish --git-dir "$1" --kind "$2" --run-id "$3" \
      --owner "$4" 2>&1)"; then
    log "could not finish $2 run $3: $result"
    return 1
  fi
  log "$result"
}

# Fails closed when python3 or either module is missing: no marker is decided,
# so none is labeled, discarded, or abandoned, and each waits for a repaired
# install. Said in the log as well as on stderr, since an outage that only
# stderr records is one nobody finds.
stop_modules_present() {  # stop_modules_present <event> <branch>
  local module missing=0
  for module in "$STOP_MODULE" "$REPORTS_MODULE"; do
    if ! command -v python3 >/dev/null 2>&1 || [[ ! -f "$(dirname "$SCRIPT_PATH")/$module" ]]; then
      log "python3 or $module is missing; no pending label resolves until it is installed"
      log_outcome "$1" "" "$2" "" "FAILED, python3 or $module is missing; pending labels wait"
      missing=1
    fi
  done
  return "$missing"
}

# Succeeds when <dir> holds an entry matching <pattern>. The directory stays
# quoted so a bracket or star in the repository path is literal, which a
# compgen -G pattern would expand instead.
any_marker() {  # any_marker <dir> <pattern>
  local marker
  for marker in "$1"/$2; do
    [[ -e "$marker" ]] && return 0
  done
  return 1
}

# The git dir, never the common dir, holds each report, so a worktree reads its
# own run and not a sibling's. The /qa reports are relative to the repository
# root, which init_repo made the working directory. HEAD and that root are the
# facts a version 3 report must be current against, since the module runs no git.
handle_stop() {
  init_repo
  any_marker "$MARKER_DIR" '*' || quiet_exit

  local branch git_dir head
  branch="$(git branch --show-current 2>/dev/null || true)"
  stop_modules_present Stop "$branch" || quiet_exit
  git_dir="$(git rev-parse --path-format=absolute --git-dir 2>/dev/null)"
  head="$(git rev-parse HEAD 2>/dev/null || true)"
  act_on_marker_decisions Stop "$branch" \
    stop "$MARKER_DIR" "$git_dir" ".gstack/qa-reports" "$(date +%s)" "$head" "$REPO_ROOT"
}

# ---------------------------------------------------------------------
# SubagentStop
# ---------------------------------------------------------------------

# Says so when the acceptance-verifier agent finished and wrote no report.
#
# A session loads its agents from the plugin cache, and an agent older than
# plugin 4.3.1 writes no acceptance-report.json. handle_stop cannot
# tell that from a run still going, so it kept the marker and logged nothing
# until the TTL. On 2026-09-09 a grading of tadw-rai returned ACCEPTED that way
# and the label was applied by hand.
#
# Measured on Claude Code 2.1.295: this event fires in the calling session's
# directory when a subagent finishes, and never for a session killed mid-run.
# last_assistant_message arrived empty, so the verdict is not read from it.
#
# It applies no label. A report that did arrive is handle_stop's to read.
#
# Only a marker this session wrote is cleared. Measured on 2026-10-09: the
# payload carries the calling session's session_id, the same one PreToolUse
# and UserPromptSubmit carry. Without the match, one agent finishing would
# clear the marker of a run interrupted earlier, or of a run in another
# worktree, and log this cause for a run it knows nothing about. A marker
# with no session line predates that line and is left for the TTL.
handle_subagent_stop() {
  local agent_type session branch git_dir
  agent_type="$(echo "$payload" | jq -r '.agent_type // empty' 2>/dev/null || true)"
  [[ "$agent_type" == *"$ACCEPTANCE_AGENT" ]] || quiet_exit
  session="$(payload_session)"
  [[ -n "$session" ]] || quiet_exit

  init_repo
  any_marker "$MARKER_DIR" 'accepted__*' || quiet_exit
  branch="$(git branch --show-current 2>/dev/null || true)"
  stop_modules_present SubagentStop "$branch" || quiet_exit
  git_dir="$(git rev-parse --path-format=absolute --git-dir 2>/dev/null)"
  act_on_marker_decisions SubagentStop "$branch" \
    subagent-stop "$MARKER_DIR" "$git_dir" "$session"
}

# ---------------------------------------------------------------------
# --doctor
# ---------------------------------------------------------------------

# Answers the question the log answers after the fact, before the fact: on this
# branch, right now, would a labeled skill find its bead? It resolves and
# prints, and writes nothing at all: no label, no export, no marker, no log
# line. bd show is the only tracker call it makes, and that is read-only.
#
# The commands below are the ones a person types. Each is put through the same
# skill_for_command and classify_skill the hook uses, so a command that has
# stopped mapping reports "not labeled" here rather than quietly diverging from
# a second list kept in step by hand.
DOCTOR_COMMANDS="build simplify code-review fresh-eyes-cr qa quality-gates verify-acceptance"

run_doctor() {
  local branch command skill claim_note

  init_repo
  branch="$(git branch --show-current 2>/dev/null || true)"

  echo "repository: $REPO_ROOT"
  echo "script:     $SCRIPT_PATH ($(script_hash))"
  echo "branch:     ${branch:-<none, detached HEAD>}"

  if ! resolve_bead "" "$branch"; then
    if [[ -n "$RESOLVE_FAILURE" ]]; then
      echo "bead:       FAILED, $RESOLVE_FAILURE."
      echo "            No bead can be resolved until that is fixed,"
      echo "            so every labeled skill run here would label nothing."
      return 0
    fi
    echo "bead:       none. No candidate from this branch resolved to a bead,"
    echo "            so every labeled skill run here would label nothing."
    return 0
  fi

  echo "bead:       $RESOLVED_ID"
  echo "labels:     $(echo "$RESOLVED_JSON" | jq -r 'if type == "array" then .[0] else . end | ((.labels // []) | join(", ")) | if . == "" then "<none>" else . end' 2>/dev/null || echo "<unreadable>")"
  echo "status:     $(echo "$RESOLVED_JSON" | jq -r 'if type == "array" then .[0] else . end | .status // "<unreadable>"' 2>/dev/null || echo "<unreadable>")"
  echo
  echo "What each labeled command would do here:"
  for command in $DOCTOR_COMMANDS; do
    if ! skill="$(skill_for_command "$command")"; then
      printf '  /%-18s not labeled\n' "$command"
      continue
    fi
    if ! classify_skill "$skill"; then
      printf '  /%-18s not labeled (%s is unmapped)\n' "$command" "$skill"
      continue
    fi
    claim_note=""
    [[ -n "${CLAIM:-}" ]] && claim_note="claim it if open (refuse it if another session holds it), then "
    if has_label "$RESOLVED_JSON" "$LABEL"; then
      if [[ -n "$claim_note" ]]; then
        printf '  /%-18s claim it if open; %s already carries "%s"\n' "$command" "$RESOLVED_ID" "$LABEL"
      else
        printf '  /%-18s nothing; %s already carries "%s"\n' "$command" "$RESOLVED_ID" "$LABEL"
      fi
    else
      printf '  /%-18s %s%s "%s"\n' "$command" "$claim_note" "$(doctor_verb "$MODE")" "$LABEL"
    fi
  done
}

# What a mode does, in the words a person would use for it.
doctor_verb() {
  case "$1" in
    apply) echo "add" ;;
    gate)  echo "wait for its report, then add" ;;
    *)     echo "ask the run to add" ;;
  esac
}

# ---------------------------------------------------------------------

# Guarded ahead of the payload read on purpose. Everything below blocks on
# stdin, and --doctor is run from a terminal where no hook payload is coming.
if [[ "${1:-}" == "--doctor" ]]; then
  run_doctor
  exit 0
fi

payload="$(cat)"
event="$(echo "$payload" | jq -r '.hook_event_name // empty' 2>/dev/null || true)"

case "$event" in
  PreToolUse)       handle_pre "$payload" ;;
  UserPromptSubmit) handle_prompt "$payload" ;;
  Stop)             handle_stop ;;
  SubagentStop)     handle_subagent_stop ;;
  *)                quiet_exit ;;
esac

exit 0
