#!/usr/bin/env bash
# Mentor loop: Claude Code directs and reviews, Muse implements, in one Muse session.
# A plain-shell alternative to the Claude Code plugin. Run it from any terminal:
#
#   scripts/mentor-loop.sh [-n ROUNDS] [-w REPO] "goal text"
#   scripts/mentor-loop.sh [-n ROUNDS] [-w REPO] -f goal.md
#   scripts/mentor-tmux.sh  [same args]   # the same, in a tmux session with a status pane and a steer pane
#
# Each agent runs in its own official harness (`claude -p`, `muse exec`), as the operator
# required. Continuity is by session id on both sides: Muse's `--session-id` was verified
# 2026-10-24 to keep context across separate `muse exec` processes.
#
# The mentor is read-only on purpose (Edit/Write disallowed; only git inspection allowed) and
# ends every reply with a `VERDICT: APPROVE` or `VERDICT: REVISE` line. Anything else counts
# as REVISE, so a mentor that rambles cannot approve by accident.
#
# Muse runs headless with `--approval-mode never`. NOTE (tested 2026-10-24): `muse exec` does
# NOT stop for approval in any mode. In the default `on-request` it ran `curl` and wrote
# ~/probe-file outside the workspace with no prompt, so its "sandbox ON" default is no boundary
# on this box. Nobody, human or Claude, is asked. Run this against repos you would let Muse
# edit freely, and treat Muse as having your full file and network access.
set -euo pipefail

ROUNDS=4
REPO=$PWD
GOAL=
LOG=
while getopts "n:w:f:l:" o; do
  case $o in
    n) ROUNDS=$OPTARG ;;
    w) REPO=$(cd "$OPTARG" && pwd) ;;
    f) GOAL=$(<"$OPTARG") ;;
    l) LOG=$OPTARG ;; # log dir; scripts/mentor-tmux.sh passes one so its panes can find it
    *) exit 2 ;;
  esac
done
shift $((OPTIND - 1))
[[ -n $GOAL ]] || GOAL=${1:-}
[[ -n $GOAL ]] || { echo "usage: $0 [-n rounds] [-w repo] (-f file | \"goal\")" >&2; exit 2; }
git -C "$REPO" rev-parse --git-dir >/dev/null  # the diff is the mentor's evidence

MUSE_ID=$(uuidgen)
CLAUDE_ID=$(uuidgen)
[[ -n $LOG ]] || LOG=$(mktemp -d "${TMPDIR:-/tmp}/mentor-loop.XXXXXX")
mkdir -p "$LOG"
echo ">> repo $REPO, muse session $MUSE_ID, claude session $CLAUDE_ID, logs $LOG"

# Read-only mentor. --allowedTools is an allowlist; everything else is refused in -p mode.
# The tool flags are variadic and would swallow a trailing prompt, so the prompt goes on stdin.
mentor() { # $1 = prompt, $2 = "first" on the first call
  local idflag=(--resume "$CLAUDE_ID")
  [[ ${2:-} == first ]] && idflag=(--session-id "$CLAUDE_ID")
  (cd "$REPO" && claude -p "${idflag[@]}" \
    --allowedTools "Read,Grep,Glob,Bash(git diff:*),Bash(git status:*),Bash(git log:*)" \
    --disallowedTools "Edit,Write,NotebookEdit" <<<"$1")
}

# Operator steering: anything appended to $LOG/steer.md is added to the next prompt, then cleared.
steer() {
  [[ -s $LOG/steer.md ]] || return 0
  printf '\n\nThe operator interjects (follow this over the earlier plan where they conflict):\n%s\n' \
    "$(<"$LOG/steer.md")"
  : >"$LOG/steer.md"
}

muse_run() { # stdout is the answer; muse's own notices go to stderr
  muse exec --workspace "$REPO" --session-id "$MUSE_ID" \
    --approval-mode never --max-model-steps 60 "$1" 2>"$LOG/muse.stderr"
}

echo ">> mentor: writing the task ticket"
TICKET=$(mentor "You are the mentor in a mentor loop. An implementer (a separate coding agent) will do the work; you never edit files. Goal:

$GOAL

Inspect the repo as needed, then write the task ticket for the implementer: concrete steps, files, and an acceptance check it can run itself. Output only the ticket." first)
printf '%s\n' "$TICKET" >"$LOG/ticket.md"

PROMPT="You are the implementer. Do the work in this repo and run the acceptance check yourself. Reply with a short summary of what you changed and the check's result.

$TICKET"

for ((r = 1; r <= ROUNDS; r++)); do
  echo ">> round $r/$ROUNDS: muse implements"
  REPORT=$(muse_run "$PROMPT$(steer)")
  printf '%s\n' "$REPORT" >"$LOG/muse-$r.md"

  echo ">> round $r/$ROUNDS: mentor reviews"
  REVIEW=$(mentor "$(steer)The implementer reports:

$REPORT

Review its work yourself: git diff / git status, read the files, judge against the ticket. Do not trust the report. If it is acceptable, give a one-line reason. If not, list exactly what to fix. The LAST line of your reply must be exactly 'VERDICT: APPROVE' or 'VERDICT: REVISE'.")
  printf '%s\n' "$REVIEW" >"$LOG/review-$r.md"

  if [[ $(tail -n1 <<<"$REVIEW" | tr -d '[:space:]*') == "VERDICT:APPROVE" ]]; then
    echo ">> approved in round $r"
    printf '%s\n' "$REVIEW"
    exit 0
  fi
  PROMPT="The mentor reviewed your work and wants changes. Fix exactly this, re-run the acceptance check, and reply with a short summary:

$REVIEW"
done

echo ">> NOT approved after $ROUNDS rounds; last review follows (logs: $LOG)" >&2
printf '%s\n' "$REVIEW" >&2
exit 1
