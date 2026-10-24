#!/usr/bin/env python3
"""muse-bridge: hold one `muse serve` (MSP) session and let short-lived clients drive it.

Why it exists: a Claude Code plugin's `$.process.spawn` cannot keep a child's stdin open, and
MSP needs a live two-way pipe (approval requests come in, `approval/decide` goes out). This
daemon owns that pipe; the plugin, a tmux pane or a human at a shell talk to it through a unix
socket with one-shot commands. The Muse session outlives each client.

  muse-bridge.py serve -w REPO [-n NAME] [--auto-allow git,ls,...]   # daemon, keep running
  muse-bridge.py ask   [-n NAME] "prompt"      # one turn; streams events as JSON lines, returns at
                                               # the first approval, the end, or --max-wait seconds
  muse-bridge.py wait  [-n NAME]               # reattach to the running turn (after decide)
  muse-bridge.py decide [-n NAME] ID allow|deny [feedback...]
  muse-bridge.py steer [-n NAME] "note"        # into the running turn
  muse-bridge.py pending|status|interrupt|stop [-n NAME]

Approvals: Muse runs in `promptUnmatched` mode, so anything its own policy does not already
allow comes here as a request. A request is auto-allowed only when it is a shell command whose
EVERY stage's argv[0] is in `--auto-allow` and whose text has no shell metacharacters or `~`/`..`
path escapes; everything else waits for `decide` (an `ask` client sees it as an `approval`
event). Tested 2026-10-24 against muse 1.4.12: a Reject really blocks the command.
"""
import argparse, base64, collections, json, mimetypes, shlex, os, re, select, shutil, socket, subprocess, sys, threading, time, uuid, queue


# systemd --user units do not have ~/.local/bin on PATH (the plugin starts this under systemd-run)
MUSE = shutil.which("muse") or os.path.expanduser("~/.local/bin/muse")


def uuid7():
    b = bytearray(int(time.time() * 1000).to_bytes(6, "big") + os.urandom(10))
    b[6] = (b[6] & 0x0F) | 0x70
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(b)))


# Never allowed through a client's `decide allow` (Claude acting through the plugin) without
# `decide --force`, which only a human types. An allow covers every stage of a compound command
# (`a && b`): it was judged as one text. Not a sandbox; a tripwire for the obvious.
RISKY = re.compile(r"\.ssh|\.gnupg|\.config/secrets|api\.env|\.env\b|\bsudo\b|\bsu\b|rm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME)|"
                   r"\bchmod\s+-R\b|\bdd\b|\bmkfs|curl[^|]*\|\s*(ba)?sh|systemctl|\bgit\s+push\b|--force")
# A path argument outside the workspace is risky too, unless it is a system location commands
# read from all the time.
SYSTEM_PATHS = ("/usr/", "/bin/", "/sbin/", "/lib", "/dev/", "/proc/self/")


def leaves_workspace(cmd, workspace):
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return True  # unparseable: ask a human
    ws = os.path.realpath(workspace)
    for t in toks:
        if not (t.startswith(("/", "~")) or ".." in t.split("/")):
            continue
        path = os.path.normpath(os.path.join(ws, os.path.expanduser(t)))
        if path == ws or path.startswith(ws + os.sep) or path.startswith(SYSTEM_PATHS):
            continue
        return True
    return False


def norm(cmd):
    """A command as the denial memory compares it: whitespace, quoting and a leading ./ do not matter,
    so `rm -rf build`, `rm  -rf ./build` and `rm -rf "build/"` are one command."""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return " ".join(cmd.split())
    return " ".join(t[2:] if t.startswith("./") else t.rstrip("/") or t for t in toks)


def sock_path(name):
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), f"muse-bridge-{name}.sock")


# ---------------------------------------------------------------- daemon

class Bridge:
    def __init__(self, workspace, auto_allow, mode, name):
        self.workspace, self.auto_allow, self.mode, self.name = workspace, set(auto_allow), mode, name
        self.p = subprocess.Popen([MUSE, "serve", "--no-session-log"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.wlock = threading.Lock()
        self.nid = 0
        self.waiters = {}      # rpc id -> queue for the response
        self.subs = []         # queues of `ask` clients (events fan out to all)
        self.pending = {}      # approvalId -> params, awaiting `decide`
        self.repo_cache, self.repo_at = "", 0.0
        self.denied = set()    # commands the operator denied; a retry of one is denied again unasked
        self.allow_all = set() # approvalIds whose later stages (a && b) were allowed with the first
        self.turn_id = None
        self.sid = None
        self.last = {"type": "idle"}  # the last turn's final message + outcome, for `wait`
        self.final_text = ""
        self.log = collections.deque(maxlen=300)  # recent events with a seq, for `snapshot` (the pane)
        self.seq = 0
        self.log_lock = threading.Lock()  # emit() runs on the reader, decide threads and client handlers
        threading.Thread(target=self.reader, daemon=True).start()
        self.rpc("initialize", {"clientInfo": {"name": "muse_bridge", "version": "0.1.0"}})
        self.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        r = self.rpc("session/start", {"commandId": uuid7(), "workspaceRoot": workspace, "approvalMode": mode})
        self.sid = r["session"]["sessionId"]

    def send(self, o):
        with self.wlock:
            self.p.stdin.write(json.dumps(o) + "\n")
            self.p.stdin.flush()

    def rpc(self, method, params, timeout=60):
        with self.wlock:
            self.nid += 1
            i = self.nid
        q = self.waiters[i] = queue.Queue()
        self.send({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        m = q.get(timeout=timeout)
        del self.waiters[i]
        if "error" in m:
            raise RuntimeError(m["error"].get("message", str(m["error"])))
        return m.get("result", {})

    def emit(self, ev):
        with self.log_lock:
            self.seq += 1
            self.log.append({**ev, "seq": self.seq, "t": int(time.time())})
        for q in list(self.subs):
            q.put(ev)

    def safe(self, subject):
        """True when this approval is routine enough to allow without asking."""
        if subject.get("kind") != "shell" or not self.auto_allow:
            return False
        if re.search(r"[;&|`$<>(){}\n\\~]|\.\./", subject.get("command", "")):
            return False
        stages = subject.get("stages") or []
        return bool(stages) and all(s.get("argv", [""])[0] in self.auto_allow for s in stages)

    def choose(self, a, allow):
        want = ("approved",) if allow else ("denied", "abort")
        return next((c for c in a["availableChoices"] if c["decision"] in want), None)

    def decide(self, a, allow, feedback=None):
        c = self.choose(a, allow)
        if c is None:
            raise RuntimeError("no matching choice offered")
        p = {"approvalId": a["approvalId"], "choiceId": c["choiceId"], "commandId": uuid7(),
             "requirementId": a["currentRequirementId"], "sessionId": self.sid}
        if feedback and c.get("acceptsFeedback"):
            p["feedback"] = feedback
        self.rpc("approval/decide", p)
        self.pending.pop(a["approvalId"], None)  # now, not at approval/resolved: `wait` must not re-announce it

    def repo_state(self):
        """`git status -sb` + diff stat of the workspace, here so the pane polls with one process.
        Cached for 2 s: a slow git on a big repo must not make every poll wait for it."""
        if time.time() - self.repo_at < 2:
            return self.repo_cache
        parts = []
        for args in (["status", "-sb", "--porcelain"], ["diff", "--stat", "HEAD"]):
            try:
                r = subprocess.run(["git", "-C", self.workspace, *args], capture_output=True, text=True, timeout=3)
                parts.append(r.stdout.strip())
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.repo_cache, self.repo_at = "\n".join(p for p in parts if p), time.time()
        return self.repo_cache

    def is_risky(self, a):
        subj = a.get("subject") or {}
        cmd = subj.get("command", "")
        return subj.get("kind") != "shell" or bool(RISKY.search(cmd)) or leaves_workspace(cmd, self.workspace)

    def approval_event(self, a):
        subj = a.get("subject", {})
        return {"type": "approval", "id": a["approvalId"], "kind": subj.get("kind"),
                "summary": subj.get("command") or json.dumps(subj)[:300],
                "workspace": subj.get("workspaceRoot"), "judgeEscalated": a.get("judgeEscalated", False),
                "risky": self.is_risky(a),
                "choices": [c["label"] for c in a["availableChoices"]]}

    def reader(self):
        for line in self.p.stdout:
            m = json.loads(line)
            meth = m.get("method")
            if meth == "approval/request":
                a = m["params"]
                self.send({"jsonrpc": "2.0", "id": m["id"], "result": {}})  # presentation receipt
                subj = a.get("subject", {})
                if subj.get("command") and norm(subj["command"]) in self.denied:
                    threading.Thread(target=self.decide, args=(a, False, "The operator already denied this exact command. "
                                     "Do not retry it or a variation; continue without it."), daemon=True).start()
                    self.emit({"type": "auto_denied", "command": subj["command"]})
                elif a["approvalId"] in self.allow_all:  # next stage of a command already vetted whole
                    threading.Thread(target=self.decide, args=(a, True), daemon=True).start()
                    self.emit({"type": "auto_allowed", "command": "(next stage) " + subj.get("command", "")})
                elif self.safe(subj):
                    threading.Thread(target=self.decide, args=(a, True), daemon=True).start()
                    self.emit({"type": "auto_allowed", "command": subj.get("command")})
                else:
                    self.pending[a["approvalId"]] = a
                    self.emit(self.approval_event(a))
            elif meth == "approval/resolved":
                self.pending.pop(m["params"].get("approvalId"), None)
                self.emit({"type": "approval_resolved", "id": m["params"].get("approvalId"),
                           "decision": m["params"].get("decision")})
            elif meth == "turn/started":
                self.turn_id = m["params"].get("turnId")
            elif meth == "item/completed":
                it = m["params"].get("item", {})
                if it.get("kind") == "agentMessage":
                    self.final_text = it.get("text", "")
                    self.emit({"type": "message", "phase": it.get("messagePhase"), "text": it.get("text", "")})
            elif meth == "turn/completed":
                self.turn_id = None
                self.last = {"type": "done", "terminal": m["params"].get("terminal"), "text": self.final_text,
                             "error": m["params"].get("error")}
                self.emit({"type": "done", "terminal": m["params"].get("terminal"),
                           "error": m["params"].get("error"), "durationMs": m["params"].get("durationMs")})
            elif "id" in m and m["id"] in self.waiters:
                self.waiters[m["id"]].put(m)

    # ---- client ops
    def handle(self, conn, req):
        op = req.get("op")
        out = lambda o: conn.sendall((json.dumps(o) + "\n").encode())
        if op in ("ask", "wait"):
            q = queue.Queue()
            self.subs.append(q)
            try:
                if op == "ask":
                    self.final_text = ""
                    self.emit({"type": "prompt", "text": req["text"]})
                    parts = [{"type": "text", "text": req["text"]}]
                    for path in req.get("images", []):  # MSP carries images inline; other files go by path in the text
                        mt = mimetypes.guess_type(path)[0] or ""
                        if not mt.startswith("image/"):
                            return out({"ok": False, "error": f"{path}: not an image; give non-images by path in the prompt"})
                        with open(path, "rb") as f:
                            parts.append({"type": "image", "mediaType": mt, "base64Data": base64.b64encode(f.read()).decode()})
                    self.rpc("turn/start", {"commandId": uuid7(), "sessionId": self.sid, "input": parts})
                elif self.turn_id is None:
                    return out(self.last)  # nothing running: the last outcome
                for a in list(self.pending.values()):  # a waiter must not miss a request already open
                    out(self.approval_event(a))
                while True:
                    ev = q.get()
                    out(ev)
                    if ev["type"] == "done":
                        break
            finally:
                self.subs.remove(q)
        elif op == "decide":
            a = self.pending.get(req["id"])
            if not a:
                return out({"ok": False, "error": "no such pending approval"})
            cmd = (a.get("subject") or {}).get("command", "")
            if req["allow"] and not req.get("force") and self.is_risky(a):
                return out({"ok": False, "needsHuman": True,
                            "error": f"not allowed without a human: {cmd or a.get('subject', {}).get('kind')!r}; "
                                     "ask the operator to run: muse-bridge.py decide --force"})
            if req["allow"] and req.get("all", True):
                self.allow_all.add(req["id"])  # the tripwire above judged the whole command text
            self.decide(a, req["allow"], req.get("feedback"))
            if not req["allow"] and req.get("by") == "operator":
                self.denied.add(norm(cmd))
            self.emit({"type": "decided", "id": req["id"], "allow": req["allow"], "by": req.get("by", "client"),
                       "command": cmd})
            out({"ok": True})
        elif op == "steer":
            if not self.turn_id:
                return out({"ok": False, "error": "no running turn"})
            self.rpc("turn/steer", {"commandId": uuid7(), "sessionId": self.sid, "expectedTurnId": self.turn_id,
                                    "input": [{"type": "text", "text": req["text"]}]})
            out({"ok": True})
        elif op == "interrupt":
            self.rpc("turn/interrupt", {"commandId": uuid7(), "sessionId": self.sid})
            out({"ok": True})
        elif op == "pending":
            out({"pending": [{"id": k, "summary": (v.get("subject") or {}).get("command"),
                              "workspace": (v.get("subject") or {}).get("workspaceRoot")}
                             for k, v in self.pending.items()]})
        elif op == "snapshot":
            out({"diff": self.repo_state(), "session": self.sid, "workspace": self.workspace, "running": self.turn_id is not None,
                 "seq": self.seq, "events": list(self.log)[-int(req.get("n", 40)):],
                 "pending": [{"id": k, **self.approval_event(v)} for k, v in self.pending.items()]})
        elif op == "status":
            out({"session": self.sid, "workspace": self.workspace, "mode": self.mode,
                 "running": self.turn_id is not None, "pending": len(self.pending),
                 "autoAllow": sorted(self.auto_allow)})
        elif op == "stop":
            out({"ok": True})
            try:
                os.unlink(sock_path(self.name))
            except OSError:
                pass
            os._exit(0)
        else:
            out({"ok": False, "error": f"unknown op {op!r}"})


def serve(args):
    path = sock_path(args.name)
    if os.path.exists(path):
        try:
            socket.socket(socket.AF_UNIX).connect(path)
            sys.exit(f"muse-bridge '{args.name}' already running at {path}")
        except OSError:
            os.unlink(path)  # stale socket from a dead daemon
    b = Bridge(os.path.abspath(args.workspace), [x for x in args.auto_allow.split(",") if x], args.mode, args.name)
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    os.chmod(path, 0o600)  # whoever can reach this socket can approve Muse's commands
    srv.listen(8)
    print(f">> muse-bridge '{args.name}': session {b.sid}, workspace {b.workspace}, socket {path}", flush=True)

    def client(conn):
        try:
            line = conn.makefile().readline()
            if line:
                b.handle(conn, json.loads(line))
        except Exception as e:  # report to the client; a bad request must not kill the daemon
            try:
                conn.sendall((json.dumps({"ok": False, "error": str(e)}) + "\n").encode())
            except OSError:
                pass
        finally:
            conn.close()

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=client, args=(conn,), daemon=True).start()


# ---------------------------------------------------------------- client

def call(name, req):
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(sock_path(name))
    except OSError:
        sys.exit(f"muse-bridge '{name}' is not running (start: muse-bridge.py serve -w REPO -n {name})")
    s.sendall((json.dumps(req) + "\n").encode())
    stop_early = req.get("op") in ("ask", "wait")
    end = time.time() + req.get("maxWait", 540)
    s.settimeout(1)
    buf = b""
    while True:
        try:
            chunk = s.recv(65536)
            if not chunk:
                return
            buf += chunk
        except socket.timeout:
            if stop_early and time.time() > end:
                print(json.dumps({"type": "still_running"}), flush=True)  # turn goes on; `wait` reattaches
                return
            continue
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            print(line.decode(), flush=True)
            if stop_early and json.loads(line).get("type") in ("approval", "done", "idle"):
                return


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve")
    sp.add_argument("-w", "--workspace", required=True)
    sp.add_argument("--auto-allow", default="", help="comma list of argv[0] allowed without asking")
    sp.add_argument("--mode", default="promptUnmatched", choices=["promptUnmatched", "onRequest", "denyUnmatched"])
    for c in ("ask", "wait", "decide", "steer", "pending", "status", "snapshot", "interrupt", "stop"):
        x = sub.add_parser(c)
        if c in ("ask", "steer"):
            x.add_argument("text")
        if c == "decide":
            x.add_argument("id")
            x.add_argument("verdict", choices=["allow", "deny"])
            x.add_argument("feedback", nargs="*")
            x.add_argument("--force", action="store_true", help="human override of the risky-command tripwire")
            x.add_argument("--by", default="client", help="who decided, for the event log")
    for k, x in sub.choices.items():
        x.add_argument("-n", "--name", default="default")
        if k == "ask":
            x.add_argument("--image", action="append", default=[], help="attach an image file (repeatable)")
        if k in ("ask", "wait"):
            x.add_argument("--max-wait", type=int, default=540, help="seconds before returning with still_running")
    a = ap.parse_args()
    if a.cmd == "serve":
        serve(a)
    elif a.cmd == "decide":
        call(a.name, {"op": "decide", "id": a.id, "allow": a.verdict == "allow", "force": a.force, "by": a.by, "feedback": " ".join(a.feedback) or None})
    elif a.cmd == "ask":
        call(a.name, {"op": "ask", "text": a.text, "maxWait": a.max_wait,
                      "images": [os.path.abspath(i) for i in a.image]})
    elif a.cmd == "steer":
        call(a.name, {"op": "steer", "text": a.text})
    elif a.cmd == "wait":
        call(a.name, {"op": "wait", "maxWait": a.max_wait})
    else:
        call(a.name, {"op": a.cmd})


if __name__ == "__main__":
    main()
