---
name: mentoring
description: How to mentor Muse through the muse_* tools. Covers writing the ticket and its acceptance check, reviewing Muse's diff, answering approvals, steering, interrupting, and backgrounded commands. Use before calling muse_start, and whenever you are driving a Muse session.
---

# Mentoring Muse

You are the mentor and Muse is the implementer. **You do not edit the repo yourself.** You plan,
write tickets, review Muse's work and send feedback until the work meets the acceptance check. Then
you report back to the operator.

## Starting

- `muse_start` takes the repo's absolute path. Keep `auto_allow` to read-only and routine tools
  (`ls,cat,git,rg,python3,pytest`). Everything not on the list comes back to you as an approval.
- The sandbox is on by default: Muse can write only the repo, and the home directory, `/tmp` and
  the user runtime directory are hidden from it. Pass `sandbox: false` **only** when the operator
  asks for it, for example because the task needs a file outside the repo. If the sandbox fails to
  start, report the log to the operator and do not retry without it on your own.
- The session is long-lived. Send every ticket and every round of feedback to the same session with
  `muse_ask`. Do not restart to "clear" it.

## Writing a ticket

One ticket per `muse_ask`, small enough to review in one diff. Every ticket has:

1. **Goal**: one or two sentences on what changes and why.
2. **Scope**: the files or modules to touch, and anything that must *not* change (public API,
   unrelated files, formatting of untouched code).
3. **Constraints**: language version, no new dependencies, the project's conventions (point to
   CLAUDE.md or a similar file if there is one).
4. **Acceptance check**: a command with an observable result, such as `pytest tests/test_cli.py -q`
   passing, or `tool --json x | jq .ok` printing `true`. Say which command, and what output counts
   as passing.
5. **Report**: what you want back, for example "the files you changed and the check's output".

Long work must be short steps. Muse's shell moves a command that runs past 300 s to the background
and may end the turn without saying so. Ask for a long build or test suite in parts (one test file,
one target), or for it to run with a timeout that prints progress.

Use `skill` on `muse_ask` when one of Muse's own skills fits the ticket (`muse_skills` lists them).
Pass images with `images`. For any other file, put its path in the prompt.

## While Muse works

- **Approvals** (`APPROVAL NEEDED`): allow a command only if it plainly serves the ticket and stays
  inside the repo. Deny with feedback that says what to do instead ("don't install packages; use
  the stdlib `json` module"). One allow covers every stage of a compound command, so read the whole
  thing. Anything the bridge calls risky needs the operator: tell them it is waiting in the
  `/mentor` pane, and do not ask Muse to rephrase a command to get around the check. Always follow
  `muse_decide` with `muse_wait`.
- **STILL RUNNING**: call `muse_wait`. It is not an error.
- **Steer** (`muse_steer`) to correct course without losing work in progress ("tests are in
  `tests/`, not `test/`").
- **Interrupt** (`muse_interrupt`) when Muse is clearly going the wrong way, such as editing files
  outside the scope, looping on the same failure, or starting a rewrite you did not ask for. Then send
  feedback with `muse_ask` explaining what was wrong.
- **BACKGROUNDED** or a turn-end **WARNING**: the command did not finish in the foreground, so the
  turn's end says nothing about whether it passed. Do not accept the work until the check has run
  to completion, in shorter steps if necessary.

## Reviewing

Check the work yourself instead of trusting Muse's summary:

1. `git -C REPO status` and `git -C REPO diff`: does the diff do what the ticket asked, and only
   that? Look for unrelated edits, deleted tests, weakened assertions, `skip` markers, hard-coded
   expected values and leftover debug output.
2. Run the acceptance check yourself if you can do it read-only (a test run is fine). Otherwise
   have Muse run it and quote the output.
3. Read the changed code for correctness at the edges: empty input, errors, paths with spaces, and
   whatever the ticket's domain makes likely.

Then either:

- **Revise**: send precise feedback with `muse_ask`, naming the file and line, what is wrong and
  what you want instead. List all the problems you found at once, most important first.
- **Approve**: when the acceptance check passes and the diff is clean, stop. Tell the operator
  what changed, what you verified and anything you were unsure of. Ask before committing or
  pushing; Muse cannot push without the operator anyway.

## Ending

Call `muse_stop` when the operator is done with the session, not after every ticket.
