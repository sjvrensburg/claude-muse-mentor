# Findings (Muse Code 1.4.12, Claude Code 2.1.310, 2026-10-24)

## Muse
- **`muse exec` never asks for approval**, in any `--approval-mode`. With the sandbox "on" it ran
  `curl` and wrote outside the workspace. Use `muse serve`.
- `muse exec --session-id <UUID>` keeps one session across separate processes; fine for
  convenience, not for gating.
- **`muse serve` (MSP v1)**: newline-delimited JSON-RPC on stdio. `initialize` (client name must
  match `^[a-z0-9_]+$`), then an `initialized` notification, `session/start`
  (`approvalMode: "promptUnmatched"`, `workspaceRoot`), `turn/start` with `input` parts
  (`text`, `image` with `base64Data` + `mediaType`, `skill`, `video`). Export the schema with
  `muse schema generate-json-schema --out DIR`.
- Approvals arrive as a server request `approval/request` (answer it with `{}`: a receipt), then you
  decide with `approval/decide` (`choiceId` from `availableChoices`, `requirementId` =
  `currentRequirementId`). Reject is `decision: "abort"`. A compound command (`a && b`) asks once
  per stage under the same `approvalId`.
- `turn/steer` (with `expectedTurnId`) lands in the running turn: a note sent during the first of
  three commands made Muse skip the other two.
- **Muse's shell waits at most 300 s in the foreground**, then moves the command to the background
  and ends the turn with no message. Long work must be many short steps; a 660 s command shows as a
  silent "completed" at ~340 s.
- Muse retries a denied command in some runs, hence the bridge's memory of denials.
- Muse reads files outside the workspace without asking.

## Muse 1.4.14 (2026-10-31)
- A Muse skill is a turn input part: `{"type": "skill", "selector": ..., "arguments": ...}`, the
  selector taken from `skill/list` (`{sessionId}` → `skills[].selector`). An unknown selector is
  request error -32032.
- A backgrounded shell command shows as a `toolCall` item with `background: true` and
  `backgroundInitiator` (`timeout` for the 300 s limit, `user` otherwise), sent in `item/updated`. Its
  later result arrives as a `backgroundTerminalCompletion` item (`command`, `terminalStatus`, `output`).
- `turn/interrupt` is only admitted when it returns; the turn ends at `turn/completed` (`cancelled`).
- **Sandboxing with bubblewrap works**: `muse serve` runs under `bwrap` with `--ro-bind / /` and tmpfs
  over `/home`, `/tmp` and `/run/user`, with only `~/.config/muse`, `~/.local/share/muse` and the
  workspace bound back. Nested `bwrap` (Muse's own sandbox) still works inside it. It also starts with
  `~/.config/muse` read-only, but `auth.json` is rewritten on token refresh, so only `settings.json` and
  `trust.json` are bound read-only. Writing, moving or deleting them then fails (EROFS, EBUSY).
- Not tested yet: a full Muse turn inside the sandbox. The auto-mode classifier refused the
  operator-only `decide --force` needed to approve the probe command.

## Claude Code plugin API
- A hooks module is **one file per plugin**; `$` may be passed only to top-level functions of that
  file; gating hooks want a `.catch`.
- `$.process.spawn` cannot keep a child's stdin open: that is why the daemon exists.
- The plugin's one-shot bridge calls are capped at 600 s, so `ask`/`wait` return `still_running`
  after 540 s and the tool tells Claude to call `muse_wait` again (tested: an 11.8 min turn).
- **A toast cannot be dismissed.** A new toast replaces the plugin's old one, so answering swaps in
  a short "Answered". This looked exactly like a pane card that would not go away.
- Buttons have no colour prop; a `Text` child inside a `Button` colours its label.
- A plugin's `skills/<name>/SKILL.md` is picked up next to a hooks module and shows as
  `<plugin>:<name>`. `claude plugin validate` does not list skills.
- The pane's own redraw after a press took ~5 ms; delays you see are somewhere else.
