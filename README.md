# claude-muse-mentor

A mentor loop for [Claude Code](https://claude.com/claude-code) and [Muse Code](https://meta.ai):
Claude plans and reviews, Muse implements, in **one ongoing Muse session**, and Muse's permission
requests come back to Claude or to you. Each agent runs in its own official harness.

```
Claude Code (mentor)  ──muse_* tools──▶  muse-bridge.py  ──MSP over stdio──▶  muse serve (implementer)
        ▲                                     │
        └── /mentor pane: approvals, feed, steer, diff ◀── you
```

It exists because `muse exec` (the headless mode) **never asks for approval**: in its default
mode it ran `curl` and wrote outside the workspace without a prompt. `muse serve` speaks Muse's
own protocol (MSP), which sends each request to the client and honours the answer. A Claude Code
plugin cannot hold a child's stdin open, so a small daemon (`bridge/muse-bridge.py`) holds the
pipe and the plugin makes one-shot calls to it.

## What you get

- **Seven tools** for Claude: `muse_start`, `muse_ask` (with `images`), `muse_wait`,
  `muse_decide`, `muse_steer`, `muse_status`, `muse_stop`.
- **A pane** (`/mentor`): Muse working or idle, an approval card per request (yellow; red with a
  two-step confirm for risky ones), a coloured activity feed, `git status` and the diff stat, a steer
  box, Interrupt and Stop; a toast announces a new question.
- **Approvals**: Muse's own policy decides what needs asking. Anything it asks is answered by Claude
  (routine, inside the repo) or by you in the pane. A command you deny is refused again if Muse
  retries it, however it is spelled.
- **Plain-shell fallbacks** in `scripts/` (`claude -p` plus `muse exec`; see the warning above).

## Requirements

- Claude Code with plugin hooks modules and hot-reloaded mods (tested on 2.1.310).
- Muse Code CLI, logged in (tested on 1.4.12).
- Python 3, `git`, and `systemd-run --user` (the daemon runs as a transient user unit).
- Linux. Nothing here is Linux-specific beyond `systemd-run` and the unix socket path.

## Install

```bash
install -m755 bridge/muse-bridge.py ~/.local/bin/muse-bridge.py   # or set MUSE_BRIDGE to its path
claude plugin install mentor --marketplace sjvrensburg/claude-muse-mentor   # or a local clone's path; or claude --plugin-dir plugin
```

Then ask Claude to call `muse_start` on a repo, and type `/mentor` for the pane. The bridge's own
CLI works without Claude: `muse-bridge.py serve -w REPO`, then `ask`, `wait`, `decide`, `steer`,
`snapshot`, `stop` (see its header).

## Safety, honestly

- **Muse runs as you.** It can read any file you can; reading a path outside the workspace did
  not ask in testing. The bridge decides *commands*, not file reads.
- `muse_decide` refuses to allow, on Claude's say-so, a command that mentions secrets (`.ssh`,
  `.env`, ...), `sudo`, `git push`, `systemctl`, forced or recursive deletes, or any path outside the
  workspace. Only you can override that, in the pane (two presses). **This is a tripwire for the
  obvious, not a sandbox.**
- `--auto-allow` names commands Muse may run unasked (`ls,cat,git,python3`), and only for a command
  with none of `; & | < > $ ( ) { } \`, a backslash, `~`, a newline or `../` in it, and that the
  tripwire above does not flag (with `git` listed, `git push` is still asked). Anything else is
  asked, so put real work in a script.
- The bridge's socket is mode 600 in `$XDG_RUNTIME_DIR`: whoever can reach it can approve Muse's
  commands.

## Findings worth knowing

See [docs/findings.md](docs/findings.md): MSP framing, the approval flow, Muse's 300 s foreground
limit, what a plugin can and cannot do, and the toast that looked like a stuck card.

## Tests

```bash
python3 tests/test_bridge.py      # the bridge, against a fake `muse serve`
claude plugin validate plugin && claude plugin test plugin
```

MIT licensed.
