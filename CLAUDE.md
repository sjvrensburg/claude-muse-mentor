# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A mentor loop: Claude Code plans and reviews, Muse Code implements, in one long-lived Muse session.
Muse's approval requests are routed back to Claude (via tools) or to the operator (via the `/mentor` pane).
Read `docs/findings.md` before changing protocol or plugin behaviour; it records tested facts about
MSP and the Claude Code plugin API that the design depends on.

## Commands

```bash
python3 tests/test_bridge.py                     # bridge, against a fake `muse serve` in the file (plain asserts, prints "ok")
claude plugin validate plugin                    # plugin manifest/module checks
claude plugin test plugin                        # runs plugin/hooks/register.test.tsx (pane tests)
claude --plugin-dir plugin                       # load the plugin for manual testing
python3 bridge/muse-bridge.py serve -w REPO      # run the daemon by hand; then ask/wait/decide/steer/snapshot/stop
```

There is no build step, linter config, or package manager. The plugin's `tsconfig.json` and
`.claude-plugin/types/` are generated and gitignored. Individual tests: the Python file is one script
(no runner); pane tests are `test(...)` blocks in `register.test.tsx`.

## Architecture

```
Claude Code ──muse_* tools──▶ plugin (register.tsx) ──one-shot CLI calls──▶ muse-bridge.py (daemon, unix socket)
                                                                                 │ MSP: NDJSON JSON-RPC on stdio
                                                                                 ▼
                                                                            muse serve
```

- **Why a daemon:** `$.process.spawn` in a plugin cannot keep a child's stdin open, but MSP needs a live
  two-way pipe (Muse sends `approval/request`, client answers with `approval/decide`). So
  `bridge/muse-bridge.py serve` owns the pipe and listens on `$XDG_RUNTIME_DIR/muse-bridge-<name>.sock`
  (mode 600); every other subcommand is a short-lived client sending one `{"op": ...}` request.
  `muse exec` is not an option: it never asks for approval (see findings).
- **Plugin (`plugin/hooks/register.tsx`):** on `session.start` registers the seven `muse_*` tools and the
  `/mentor` command. `muse_start` launches the daemon as a transient `systemd-run --user` unit
  (`muse-bridge-mentor`, bridge name `mentor`). All bridge calls go through `bridge()` in
  `plugin/hooks/bridge.ts`, which shells out to `python3 $MUSE_BRIDGE` (default `~/.local/bin/muse-bridge.py`
  — the *installed* copy, so reinstall after editing the bridge). `bridge()` puts `-n mentor` right after
  the subcommand so callers can end options with `--`: any text from Claude or the operator (prompt, steer
  note, approval id) goes after `--`, and decide feedback goes as one `--feedback=TEXT` argument, so no word
  of it can be read as `--force` or `--by`.
- **Bridge stream:** for `ask`/`wait` the daemon closes the connection at the first approval or at `done`;
  the client only enforces the `--max-wait` deadline. If `muse serve` exits, the daemon fails waiting
  calls, emits `done` (`terminal: museExited`) and exits, so `status` stops reporting a session.
- **Event contract:** the bridge streams JSON-line events (`approval`, `message`, `done`, `still_running`,
  `auto_allowed`, `auto_denied`, `decided`, `prompt`, `idle`, ...). `digest()` turns them into text for
  Claude; `lineOf()` turns them into pane feed rows. A new or renamed event type must be handled in both,
  and in the bridge.
- **Timeouts:** plugin bridge calls are capped at 600 s, so `ask`/`wait` return `still_running` after 540 s
  (`--max-wait`) and Claude must call `muse_wait`. Separately, Muse's own shell backgrounds any command
  past 300 s and ends the turn silently.
- **Pane:** polls `snapshot` every 2 s only while a session exists (stops after 30 s without one). State
  lives in `atom`s; the `shape` tag on `snap` (`snap-1`) must be bumped when the `Snap` type in
  `plugin/types/index.d.ts` changes. Button handlers must return immediately (drop the card, then decide
  in the background).

## Approval/safety rules (bridge)

- Muse runs in `promptUnmatched` mode; its policy decides what reaches the bridge.
- `--auto-allow` passes a shell command only if every stage's argv[0] is listed, the text has no shell
  metacharacters, `~`, backslash, newline, or `../`, and `is_risky()` does not flag it.
- `RISKY` regex plus `leaves_workspace()` block a client `decide allow` unless `--force` is given; only the
  pane (operator, two presses) passes `--force`. Claude's `muse_decide` never does.
- Operator denials (`by == "operator"`) are remembered via `norm()` so respellings of a denied command
  are auto-refused. `tests/test_bridge.py` covers these, plus bypass spellings that once got through
  (`..;`, `$HOME/`, `x>/path`, `--opt=/path`, `/dev/tcp`); add each new one there.
- This is a tripwire, not a sandbox: Muse can still read files outside the workspace unasked.

## Constraints from the plugin API

- A hooks module is one file per plugin (`hooks.json` lists only `register.tsx`); `$` may be passed only to
  top-level functions of that file. Gating `tool.call` hooks end with `.catch(...)`.
- Toasts cannot be dismissed; a new toast replaces the old one (hence the short "Answered" toast).
- `Button` has no colour prop; colour a `Text` child instead.

## Shell fallbacks

`scripts/mentor-loop.sh` (and its tmux wrapper `mentor-tmux.sh`) runs `claude -p` as a read-only mentor
against `muse exec`, ending each round on a `VERDICT: APPROVE|REVISE` line. It has **no approval gating**
because `muse exec` never asks.
