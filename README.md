# claude-muse-mentor

Let [Claude Code](https://claude.com/claude-code) mentor [Muse Code](https://meta.ai) on a coding
task. Claude plans the work, hands Muse a ticket, reviews what Muse did and sends feedback, all in
**one ongoing Muse session**, until Claude is satisfied. Whenever Muse wants to run a command its own
policy doesn't already allow, the request comes back to Claude for routine work, or to you for
anything risky.

```
Claude Code (mentor)  ──muse_* tools──▶  muse-bridge.py  ──▶  muse serve (implementer)
        ▲                                     │
        └── /mentor pane: approvals, feed, steer, diff ◀── you
```

## Requirements

- Linux with `systemd --user` (the bridge runs as a user service).
- Claude Code (tested on 2.1.310).
- Muse Code CLI, logged in (tested on 1.4.12).
- Python 3 and `git`. The repository Muse works in must be a git repo.

## Install

```bash
git clone https://github.com/sjvrensburg/claude-muse-mentor
cd claude-muse-mentor
install -Dm755 bridge/muse-bridge.py ~/.local/bin/muse-bridge.py
claude plugin install mentor --marketplace sjvrensburg/claude-muse-mentor
```

Restart any open Claude Code sessions afterwards. To keep the bridge somewhere else, set
`MUSE_BRIDGE` to its path.

## Quick start

1. Open Claude Code in any directory and give it a task, naming the repo:

   > Use Muse to add a `--json` flag to the CLI in /home/me/src/mytool. Start it with auto_allow
   > `ls,cat,git`.

   Claude calls `muse_start`, writes Muse a ticket with an acceptance check, and waits.
2. Type `/mentor` to open the pane beside the chat (it needs a terminal about 110 columns wide).
3. Watch the activity feed. Claude answers routine approvals itself; anything it may not allow
   shows up as a card in the pane for you. Claude reviews Muse's diff and keeps sending feedback
   until it approves the work.
4. When you're done, ask Claude to stop Muse, or press **Stop session** in the pane.

You can attach screenshots to a task (Claude passes them to Muse as images); for any other file,
give its path in the task and Muse reads it itself.

## The `/mentor` pane

| What you see | What it means |
| --- | --- |
| **Muse is working / idle** | Whether a turn is running. The status line says the same. |
| Yellow card, **? ANSWER NEEDED** | Muse wants to run the command shown. **Allow** or **Deny** (keys `a` / `d` act on the top card). |
| Red card, **! RISKY** | It touches secrets, uses `sudo`, pushes, or reaches outside the repo. **Allow…** then **Confirm** (two presses), or **Deny**. |
| Activity feed | Claude's prompts to Muse (`CLAUDE`), Muse's replies (`MUSE`), automatic answers, and every decision. |
| Changes | `git status` and the diff stat of the repo. |
| `steer>` box | A note for the turn Muse is running now; it reads it without stopping. |
| **Interrupt** (`i`), **Stop session** (`s`) | End the current turn, or end the Muse session. |

A toast pops up when a new question arrives, even with the pane closed. If you deny a command,
Muse is refused automatically if it tries the same command again, however it is spelled.

## Approvals: who decides what

1. **Muse's own policy** lets through what it considers safe, without asking anyone.
2. **Auto-allow** (the `auto_allow` list you give at start, e.g. `ls,cat,git,python3`): a command
   whose every part starts with a listed program is allowed unasked, unless it contains shell syntax
   (`; & | < > $ ( ) { }`, backticks, `~`, `../`) or looks risky. With `git` listed, `git push` is
   still asked. Keep this list to read-only and routine tools; put real work in a script.
3. **Claude** answers everything else that is routine and inside the repo.
4. **You** answer what Claude may not allow: anything that mentions secrets (`.ssh`, `.env`, ...),
   `sudo`, `git push`, `systemctl`, forced or recursive deletes, network devices, or a path outside
   the repo. Claude cannot override this.

## Safety, honestly

- **Muse runs as you.** It can read any file you can: reading outside the repo did not ask in
  testing. The approval flow covers *commands*, not file reads.
- The risky-command check is **a tripwire for the obvious, not a sandbox.** A determined or
  confused agent can phrase a command it doesn't catch. Use this on repos and machines where that is
  acceptable.
- Whoever can reach the bridge's socket (mode 600, in `$XDG_RUNTIME_DIR`) can approve Muse's
  commands.

## Good to know

- **Long tasks:** Claude waits about 9 minutes per call, then keeps waiting on its own (you'll see
  "still running"). Muse itself gives up on any single shell command after 300 s and ends the turn
  without saying so, so long builds or test runs should be split into shorter steps.
- **Muse not starting?** Claude reports the bridge's log. To read it yourself:
  `journalctl --user -u muse-bridge-mentor -n 50`.
- **Without Claude:** the bridge has its own command line:
  `muse-bridge.py serve -w REPO`, then `ask`, `wait`, `decide`, `steer`, `status`, `stop`
  (`muse-bridge.py --help`).

## Shell-only alternative (no approvals)

`scripts/mentor-loop.sh` runs the same loop with `claude -p` and `muse exec`, and
`scripts/mentor-tmux.sh` wraps it in a tmux session with a status pane and a steer pane:

```bash
scripts/mentor-tmux.sh -n 4 -w /path/to/repo "goal text"
```

**`muse exec` never asks for approval**: in testing it ran `curl` and wrote outside the workspace
without a prompt. Only use the scripts on repos you would let Muse edit freely.

## Uninstall

```bash
claude plugin uninstall mentor
claude plugin marketplace remove claude-muse-mentor
rm ~/.local/bin/muse-bridge.py
```

## Development

How it works, and why a separate bridge process is needed, is in [CLAUDE.md](CLAUDE.md). What we
learned about Muse's protocol and the Claude Code plugin API is in
[docs/findings.md](docs/findings.md). Tests:

```bash
python3 tests/test_bridge.py      # the bridge, against a fake `muse serve`
claude plugin validate plugin && claude plugin test plugin
```

MIT licensed.
