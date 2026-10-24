#!/usr/bin/env bash
# TUI front end for mentor-loop.sh: one tmux session, reachable over SSH from the phone or iPad.
#
#   scripts/mentor-tmux.sh [-n ROUNDS] [-w REPO] "goal text"      (same args as mentor-loop.sh)
#
# Panes: left = the loop's progress; top right = what the implementer has changed so far
# (git status/diff --stat, refreshed every 2 s); bottom right = a steer prompt, where each line
# you type is handed to the next prompt of whichever agent speaks next. Detach with C-b d; the
# loop keeps running and `tmux attach -t mentor` returns to it. Logs: the directory it prints.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$PWD
args=("$@")
for ((i = 0; i < $#; i++)); do
  [[ ${args[i]} == -w ]] && REPO=$(cd "${args[i+1]}" && pwd)
done
command -v tmux >/dev/null || { echo "tmux missing" >&2; exit 1; }
tmux has-session -t mentor 2>/dev/null && { echo "session 'mentor' exists: tmux attach -t mentor" >&2; exit 1; }

LOG=$(mktemp -d "${TMPDIR:-/tmp}/mentor-loop.XXXXXX")
q() { printf '%q ' "$@"; }  # quote each arg for the shell tmux starts

tmux new-session -d -s mentor -x 200 -y 50 \
  "$(q "$HERE/mentor-loop.sh" -l "$LOG" "$@"); echo; echo '>> loop finished (exit '\$?'); press Enter to close'; read"
tmux split-window -h -t mentor "watch -n2 -t $(q "git -C $REPO status -sb; echo; git -C $REPO diff --stat HEAD")"
tmux split-window -v -t mentor \
  "echo 'steer> type a note and press Enter; the next agent prompt carries it'; while IFS= read -r -p 'steer> ' l; do printf '%s\n' \"\$l\" >> $(q "$LOG/steer.md"); done"
echo ">> logs $LOG"
tmux select-pane -t "$(tmux list-panes -t mentor -F '#{pane_id}' | head -1)"  # window index depends on base-index
exec tmux attach -t mentor
