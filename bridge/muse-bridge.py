#!/usr/bin/env python3
"""muse-bridge: hold one `muse serve` (MSP) session and let short-lived clients drive it.

Why it exists: a Claude Code plugin's `$.process.spawn` cannot keep a child's stdin open, and
MSP needs a live two-way pipe (approval requests come in, `approval/decide` goes out). This
daemon owns that pipe; the plugin, a tmux pane or a human at a shell talk to it through a unix
socket with one-shot commands. The Muse session outlives each client.

  muse-bridge.py serve -w REPO [-n NAME] [--auto-allow git,ls,...] [--sandbox]   # daemon, keep running
  muse-bridge.py ask   [-n NAME] [--skill SEL [--skill-args TEXT]] "prompt"   # one turn; streams events
                                               # as JSON lines, returns at the first approval, the end,
                                               # or --max-wait seconds
  muse-bridge.py wait  [-n NAME]               # reattach to the running turn (after decide)
  muse-bridge.py decide [-n NAME] [--feedback TEXT] ID allow|deny [feedback...]
  muse-bridge.py steer [-n NAME] "note"        # into the running turn
  muse-bridge.py pending|status|skills|interrupt|stop [-n NAME]

--sandbox runs `muse serve` under bubblewrap: the filesystem is read-only, /home, /tmp, /run/user
(this socket, ssh-agent, ...) and removable media are hidden, and only the workspace and Muse's own
state directories are writable. The network stays open: Muse needs it to reach its model.

Approvals: Muse runs in `promptUnmatched` mode, so anything its own policy does not already
allow comes here as a request. A request is auto-allowed only when it is a shell command whose
EVERY stage's argv[0] is in `--auto-allow` and whose text has no shell metacharacters or `~`/`..`
path escapes and that the risky-command tripwire does not flag; everything else waits for `decide` (an `ask` client sees it as an `approval`
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
                   r"\bchmod\s+-R\b|\bdd\b|\bmkfs|curl[^|]*\|\s*(ba)?sh|systemctl|\bgit\s+push\b|--force|/dev/(tcp|udp)/")
# A path argument outside the workspace is risky too, unless it is a system location commands
# read from all the time. Not all of /dev/: bash's /dev/tcp/HOST/PORT is a network connection. Not
# all of /proc/self/ either: /proc/self/root/ is the whole filesystem and /proc/self/environ the secrets.
SYSTEM_PATHS = ("/usr/", "/bin/", "/sbin/", "/lib/", "/lib64/", "/proc/self/fd/", "/proc/self/status", "/dev/fd/",
                "/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/stdin", "/dev/stdout", "/dev/stderr")
# A variable that names a place outside the workspace (`$HOME/x`, `${XDG_CONFIG_HOME}`), or any
# variable used as the root of a path (`$D/x`): the bridge cannot know where it points.
PATH_VAR = re.compile(r"\$\{?(HOME|USER|TMPDIR|OLDPWD|XDG_\w+)\b|^\$\{?\w+\}?/")
# Brace expansion builds a path the bridge never sees as text: `{.,.}.` is `..`.
BRACES = re.compile(r"\{[^{}]*(,|\.\.)[^{}]*\}")


def leaves_workspace(cmd, workspace):
    if "$'" in cmd:  # ANSI-C quoting: $'\x2e\x2e/x' is ../x, spelled so no check below sees it
        return True
    try:
        # punctuation_chars splits `x>/tmp/out` and `..;` the way the shell does
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return True  # unparseable: ask a human
    ws = os.path.realpath(workspace)
    for i, tok in enumerate(toks):
        if BRACES.search(tok):
            return True
        if tok in ("cd", "pushd"):  # `cd`, `cd -`, `cd -P`: to $HOME, $OLDPWD or somewhere not named
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            if not nxt or nxt.startswith("-") or set(nxt) <= set("();<>|&"):
                return True
        for t in tok.split("="):  # --directory=/x, of=/x, --directory=$D/x
            if PATH_VAR.search(t):
                return True
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


def sandbox_argv(workspace, muse, env=os.environ, exists=os.path.exists):
    """The bubblewrap prefix that runs `muse` with only the workspace and Muse's own state writable.
    What it hides it replaces with an empty tmpfs; later binds put back the few paths Muse needs."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("--sandbox needs bubblewrap (bwrap) on PATH")
    home = os.path.realpath(env.get("HOME") or os.path.expanduser("~"))
    ws = os.path.realpath(workspace)
    if ws == "/" or home == ws or home.startswith(ws + os.sep):
        raise RuntimeError(f"--sandbox: the workspace {ws} contains the home directory, so there is nothing to hide")
    xdg = lambda var, default: os.path.realpath(env.get(var) or os.path.join(home, default))
    hide = ["/home", "/tmp", "/var/tmp", "/run/user", "/mnt", "/media", "/run/media", home,
            env.get("XDG_RUNTIME_DIR") or ""]
    rw = [os.path.join(xdg("XDG_CONFIG_HOME", ".config"), "muse"), os.path.join(xdg("XDG_DATA_HOME", ".local/share"), "muse"),
          os.path.join(xdg("XDG_STATE_HOME", ".local/state"), "muse"), os.path.join(xdg("XDG_CACHE_HOME", ".cache"), "muse")]
    ro = [os.path.join(home, ".gitconfig"), os.path.join(xdg("XDG_CONFIG_HOME", ".config"), "git")]
    muse_dir = os.path.dirname(os.path.realpath(muse))
    if muse_dir == home or muse_dir.startswith(home + os.sep):
        ro.append(muse_dir)  # ~/.local/bin: muse itself, and tools installed beside it
    argv = [bwrap, "--die-with-parent", "--unshare-pid", "--unshare-ipc", "--ro-bind", "/", "/",
            "--dev", "/dev", "--proc", "/proc"]
    seen = set()
    for d in hide:
        d = os.path.realpath(d) if d else ""
        if d and d not in seen and exists(d):
            seen.add(d)
            argv += ["--tmpfs", d]
    for d in ro:
        if exists(d):
            argv += ["--ro-bind", d, d]
    for d in rw:
        if exists(d):
            argv += ["--bind", d, d]
    # Muse's policy files stay read-only on top of its writable config (auth.json must refresh), so
    # it cannot loosen its own approval rules. A mount point cannot be replaced or deleted either.
    for f in ("settings.json", "trust.json"):
        f = os.path.join(rw[0], f)
        if exists(f):
            argv += ["--ro-bind", f, f]
    return argv + ["--bind", ws, ws, "--chdir", ws]  # last: the workspace may sit under a hidden path


def background_command(item):
    """The shell command of a backgrounded `toolCall` item, as well as the item names it."""
    try:
        args = json.loads(item.get("args") or "{}")
    except ValueError:
        args = {}
    if isinstance(args, dict):
        for k in ("command", "cmd"):
            if isinstance(args.get(k), str):
                return args[k]
    return (item.get("displayLabel") or {}).get("active") or item.get("tool") or "(a command)"


# ---------------------------------------------------------------- daemon

class Bridge:
    def __init__(self, workspace, auto_allow, mode, name, sandbox=False):
        self.workspace, self.auto_allow, self.mode, self.name = workspace, set(auto_allow), mode, name
        self.sandbox = sandbox
        prefix = sandbox_argv(workspace, MUSE) if sandbox else []
        # muse's stderr goes where ours does (the unit's journal): the only trace when it fails
        self.p = subprocess.Popen([*prefix, MUSE, "serve", "--no-session-log"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True, bufsize=1)
        self.wlock = threading.Lock()
        self.dlock = threading.Lock()  # one decide per approval, however many clients answer it
        self.tlock = threading.Lock()  # one turn at a time
        self.nid = 0
        self.waiters = {}      # rpc id -> queue for the response
        self.subs = []         # queues of `ask` clients (events fan out to all)
        self.pending = {}      # approvalId -> params, awaiting `decide`
        self.repo_cache, self.repo_at = "", 0.0
        self.denied = set()    # commands the operator denied; a retry of one is denied again unasked
        self.allow_all = set() # approvalIds whose later stages (a && b) were allowed with the first
        self.turn_id = None
        self.starting = False  # turn/start sent, turn/started not yet seen
        self.sid = None
        self.last = {"type": "idle"}  # the last turn's final message + outcome, for `wait`
        self.final_text, self.final_is_answer = "", False
        self.backgrounded = {}  # itemId -> command Muse's shell moved to the background this turn
        self.log = collections.deque(maxlen=300)  # recent events with a seq, for `snapshot` (the pane)
        self.seq = 0
        self.log_lock = threading.Lock()  # emit() runs on the reader, decide threads and client handlers
        threading.Thread(target=self.reader, daemon=True).start()
        self.rpc("initialize", {"clientInfo": {"name": "muse_bridge", "version": "0.1.0"}})
        self.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        r = self.rpc("session/start", {"commandId": uuid7(), "workspaceRoot": workspace, "approvalMode": mode})
        self.sid = r["session"]["sessionId"]

    @property
    def running(self):
        return self.turn_id is not None or self.starting

    def send(self, o):
        with self.wlock:
            self.p.stdin.write(json.dumps(o) + "\n")
            self.p.stdin.flush()

    def rpc(self, method, params, timeout=60):
        with self.wlock:
            self.nid += 1
            i = self.nid
        q = self.waiters[i] = queue.Queue()
        try:
            self.send({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
            m = q.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(f"{method}: no answer from muse in {timeout} s") from None
        finally:
            self.waiters.pop(i, None)
        if "error" in m:
            raise RuntimeError(m["error"].get("message", str(m["error"])))
        return m.get("result", {})

    def emit(self, ev):
        with self.log_lock:
            self.seq += 1
            self.log.append({**ev, "seq": self.seq, "t": int(time.time())})
        for q in list(self.subs):
            q.put(ev)

    def safe(self, a):
        """True when this approval is routine enough to allow without asking."""
        subject = a.get("subject") or {}
        if subject.get("kind") != "shell" or not self.auto_allow:
            return False
        if re.search(r"[;&|`$<>(){}\n\\~]|\.\./", subject.get("command", "")):
            return False
        if self.is_risky(a):  # `git push` is still a push when `git` is auto-allowed
            return False
        stages = subject.get("stages") or []
        return bool(stages) and all((s.get("argv") or [""])[0] in self.auto_allow for s in stages)

    def choose(self, a, allow):
        want = ("approved",) if allow else ("denied", "abort")
        return next((c for c in a.get("availableChoices", []) if c.get("decision") in want), None)

    def decide(self, a, allow, feedback=None):
        c = self.choose(a, allow)
        if c is None:
            raise RuntimeError("no matching choice offered")
        p = {"approvalId": a["approvalId"], "choiceId": c["choiceId"], "commandId": uuid7(),
             "requirementId": a["currentRequirementId"], "sessionId": self.sid}
        if feedback and c.get("acceptsFeedback"):
            p["feedback"] = feedback
        self.rpc("approval/decide", p)

    def auto_decide(self, a, allow, feedback=None):
        """An answer the bridge gives unasked. If it fails, a human must answer instead, or Muse waits forever."""
        try:
            self.decide(a, allow, feedback)
        except Exception as e:
            print(f"muse-bridge: could not answer {a.get('approvalId')!r} unasked ({e!r}); asking", file=sys.stderr, flush=True)
            self.pending[a["approvalId"]] = a
            self.emit(self.approval_event(a))

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
        subj = a.get("subject") or {}
        return {"type": "approval", "id": a["approvalId"], "kind": subj.get("kind"),
                "summary": subj.get("command") or json.dumps(subj)[:300],
                "workspace": subj.get("workspaceRoot"), "judgeEscalated": a.get("judgeEscalated", False),
                "risky": self.is_risky(a),
                "choices": [c.get("label") for c in a.get("availableChoices", [])]}

    def reader(self):
        for line in self.p.stdout:
            try:
                self.on_message(json.loads(line))
            except Exception as e:  # one odd message must not stop the session from being read
                print(f"muse-bridge: skipped a message from muse ({e!r}): {line[:300]!r}", file=sys.stderr, flush=True)
        self.muse_exited()

    def muse_exited(self):
        """Muse is gone: wake anyone waiting, then end the daemon so `status` stops reporting a session."""
        code = self.p.wait()
        print(f"muse-bridge: muse serve exited ({code})", file=sys.stderr, flush=True)
        for q in list(self.waiters.values()):  # a call muse will never answer
            q.put({"error": {"message": f"muse serve exited ({code})"}})
        self.emit({"type": "done", "terminal": "museExited", "error": f"muse serve exited ({code})"})
        time.sleep(0.5)  # let the handlers deliver it
        try:
            os.unlink(sock_path(self.name))
        except OSError:
            pass
        os._exit(1)

    def on_message(self, m):
        meth = m.get("method")
        if meth == "approval/request":
            a = m["params"]
            self.send({"jsonrpc": "2.0", "id": m["id"], "result": {}})  # presentation receipt
            subj = a.get("subject") or {}
            if subj.get("command") and norm(subj["command"]) in self.denied:
                threading.Thread(target=self.auto_decide, args=(a, False, "The operator already denied this exact command. "
                                 "Do not retry it or a variation; continue without it."), daemon=True).start()
                self.emit({"type": "auto_denied", "command": subj["command"]})
            elif a["approvalId"] in self.allow_all:  # next stage of a command already vetted whole
                threading.Thread(target=self.auto_decide, args=(a, True), daemon=True).start()
                self.emit({"type": "auto_allowed", "command": "(next stage) " + subj.get("command", "")})
            elif self.safe(a):
                threading.Thread(target=self.auto_decide, args=(a, True), daemon=True).start()
                self.emit({"type": "auto_allowed", "command": subj.get("command")})
            else:
                self.pending[a["approvalId"]] = a
                self.emit(self.approval_event(a))
        elif meth == "approval/resolved":
            self.pending.pop(m["params"].get("approvalId"), None)
            self.emit({"type": "approval_resolved", "id": m["params"].get("approvalId"),
                       "decision": m["params"].get("decision")})
        elif meth == "turn/started":
            self.turn_id, self.starting = m["params"].get("turnId"), False
        elif meth in ("item/updated", "item/completed") and (m["params"].get("item") or {}).get("background"):
            # Muse's shell gives a foreground command 300 s, then backgrounds it and may end the turn
            # without a word: say so, or the mentor reads the silence as success
            it = m["params"]["item"]
            if it.get("itemId") not in self.backgrounded:
                cmd = background_command(it)
                self.backgrounded[it.get("itemId")] = cmd
                self.emit({"type": "backgrounded", "command": cmd, "initiator": it.get("backgroundInitiator")})
        elif meth == "item/completed" and (m["params"].get("item") or {}).get("kind") == "backgroundTerminalCompletion":
            f = m["params"]["item"].get("backgroundTerminalCompletion") or {}
            self.emit({"type": "background_finished", "command": f.get("command", ""), "status": f.get("terminalStatus"),
                       "output": (f.get("output") or "")[-2000:]})
        elif meth == "item/completed":
            it = m["params"].get("item", {})
            if it.get("kind") == "agentMessage":
                # the turn's answer is its finalAnswer message; any later note must not replace it
                phase = it.get("messagePhase")
                if phase == "finalAnswer" or not self.final_is_answer:
                    self.final_text, self.final_is_answer = it.get("text", ""), phase == "finalAnswer"
                self.emit({"type": "message", "phase": phase, "text": it.get("text", "")})
        elif meth == "turn/completed":
            self.turn_id, self.starting = None, False
            # an approval belongs to its turn: one still open now can never be answered, and left in
            # `pending` it would end every later ask/wait at once
            self.pending.clear()
            self.allow_all.clear()
            bg, self.backgrounded = list(self.backgrounded.values()), {}
            self.last = {"type": "done", "terminal": m["params"].get("terminal"), "text": self.final_text,
                         "error": m["params"].get("error"), "backgrounded": bg}
            self.emit({"type": "done", "terminal": m["params"].get("terminal"), "error": m["params"].get("error"),
                       "durationMs": m["params"].get("durationMs"), "backgrounded": bg})
        elif meth is not None and "id" in m:  # a request this bridge does not know: say so, or muse waits on it
            print(f"muse-bridge: refused unsupported request {meth!r}", file=sys.stderr, flush=True)
            self.send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": f"muse_bridge does not support {meth}"}})
        elif meth is None and m.get("id") in self.waiters:
            self.waiters[m["id"]].put(m)

    @staticmethod
    def client_gone(conn):
        r, _, _ = select.select([conn], [], [], 0)
        try:
            return bool(r) and conn.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    # ---- client ops
    def handle(self, conn, req):
        op = req.get("op")
        out = lambda o: conn.sendall((json.dumps(o) + "\n").encode())
        if op in ("ask", "wait"):
            # The stream ends (the connection closes) at the first approval or at the end of the turn.
            if op == "ask":
                parts = []
                if req.get("skill"):  # a Muse skill, resolved by Muse against its catalog (`skills` lists it)
                    parts.append({"type": "skill", "selector": req["skill"],
                                  **({"arguments": req["skillArgs"]} if req.get("skillArgs") else {})})
                if req["text"] or not parts:
                    parts.append({"type": "text", "text": req["text"]})
                for path in req.get("images", []):  # MSP carries images inline; other files go by path in the text
                    mt = mimetypes.guess_type(path)[0] or ""
                    if not mt.startswith("image/"):
                        return out({"ok": False, "error": f"{path}: not an image; give non-images by path in the prompt"})
                    with open(path, "rb") as f:
                        parts.append({"type": "image", "mediaType": mt, "base64Data": base64.b64encode(f.read()).decode()})
                with self.tlock:
                    if self.running:
                        return out({"ok": False, "error": "Muse is already running a turn: wait for it, steer it or interrupt it"})
                    self.starting = True
            q = queue.Queue()
            self.subs.append(q)  # before looking at turn_id, so a turn that ends now still reaches this client
            try:
                if op == "ask":
                    self.final_text, self.final_is_answer = "", False
                    shown = (f"/{req['skill']} {req.get('skillArgs') or ''}".strip() + "\n" if req.get("skill") else "") + req["text"]
                    self.emit({"type": "prompt", "text": shown.strip()})
                    try:
                        self.rpc("turn/start", {"commandId": uuid7(), "sessionId": self.sid, "input": parts})
                    except Exception:
                        self.starting = False
                        raise
                elif not self.running:
                    return out(self.last)  # nothing running: the last outcome
                waiting = list(self.pending.values())  # a waiter must not miss a request already open
                for a in waiting:
                    out(self.approval_event(a))
                if waiting:
                    return
                while True:
                    try:
                        ev = q.get(timeout=5)
                    except queue.Empty:
                        if self.client_gone(conn):  # it gave up (--max-wait): stop holding a thread for it
                            return
                        continue
                    out(ev)
                    if ev["type"] in ("approval", "done"):
                        return
            finally:
                self.subs.remove(q)
        elif op == "decide":
            with self.dlock:  # the pane and Claude may answer the same request at once: the first one wins
                a = self.pending.get(req["id"])
                if not a:
                    return out({"ok": False, "error": "no such pending approval"})
                cmd = (a.get("subject") or {}).get("command", "")
                if req["allow"] and not req.get("force") and self.is_risky(a):
                    return out({"ok": False, "needsHuman": True,
                                "error": f"not allowed without a human: {cmd or a.get('subject', {}).get('kind')!r}; "
                                         "ask the operator to answer it in the /mentor pane"})
                del self.pending[req["id"]]
            if req["allow"] and req.get("all", True):
                self.allow_all.add(req["id"])  # the tripwire above judged the whole command text
            try:
                self.decide(a, req["allow"], req.get("feedback"))
            except Exception:
                self.allow_all.discard(req["id"])
                self.pending[req["id"]] = a  # still open: let someone answer it again
                raise
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
            if not self.running:
                return out({"ok": False, "error": "no running turn"})
            self.rpc("turn/interrupt", {"commandId": uuid7(), "sessionId": self.sid})
            self.emit({"type": "interrupted", "by": req.get("by", "client")})
            out({"ok": True})
        elif op == "skills":
            r = self.rpc("skill/list", {"sessionId": self.sid})
            short = lambda t: t if len(t) <= 200 else t[:197] + "..."
            out({"skills": [{"selector": s.get("selector"), "description": short(s.get("description") or ""),
                             **({"argumentHint": s["argumentHint"]} if s.get("argumentHint") else {})}
                            for s in r.get("skills", [])]})
        elif op == "pending":
            out({"pending": [{"id": k, "summary": (v.get("subject") or {}).get("command"),
                              "workspace": (v.get("subject") or {}).get("workspaceRoot")}
                             for k, v in list(self.pending.items())]})
        elif op == "snapshot":
            out({"diff": self.repo_state(), "session": self.sid, "workspace": self.workspace, "running": self.running,
                 "sandbox": self.sandbox,
                 "seq": self.seq, "events": list(self.log)[-int(req.get("n", 40)):],
                 "pending": [{"id": k, **self.approval_event(v)} for k, v in list(self.pending.items())]})
        elif op == "status":
            out({"session": self.sid, "workspace": self.workspace, "mode": self.mode,
                 "running": self.running, "pending": len(self.pending),
                 "autoAllow": sorted(self.auto_allow), "sandbox": self.sandbox})
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
    b = Bridge(os.path.abspath(args.workspace), [x for x in args.auto_allow.split(",") if x], args.mode, args.name,
               args.sandbox)
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    os.chmod(path, 0o600)  # whoever can reach this socket can approve Muse's commands
    srv.listen(8)
    print(f">> muse-bridge '{args.name}': session {b.sid}, workspace {b.workspace}, socket {path}"
          f"{', sandboxed' if b.sandbox else ''}", flush=True)

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
    # The daemon closes the connection when it has answered (for ask/wait: at the first approval or
    # the end of the turn). The deadline is checked on every pass, so a chatty turn cannot outlast it.
    waits = req.get("op") in ("ask", "wait")
    limit = req.get("maxWait", 540) if waits else 90
    end = time.time() + limit
    s.settimeout(1)
    buf = b""
    while time.time() < end:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            if buf.strip():
                print(buf.decode(), flush=True)
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            print(line.decode(), flush=True)
    if waits:
        print(json.dumps({"type": "still_running"}), flush=True)  # turn goes on; `wait` reattaches
    else:
        sys.exit(json.dumps({"ok": False, "error": f"no answer from the bridge in {limit} s"}))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve")
    sp.add_argument("-w", "--workspace", required=True)
    sp.add_argument("--auto-allow", default="", help="comma list of argv[0] allowed without asking")
    sp.add_argument("--mode", default="promptUnmatched", choices=["promptUnmatched", "onRequest", "denyUnmatched"])
    sp.add_argument("--sandbox", action="store_true", help="run muse under bubblewrap: only the workspace is writable, home is hidden")
    for c in ("ask", "wait", "decide", "steer", "pending", "status", "skills", "snapshot", "interrupt", "stop"):
        x = sub.add_parser(c)
        if c in ("ask", "steer"):
            x.add_argument("text")
        if c == "decide":
            x.add_argument("id")
            x.add_argument("verdict", choices=["allow", "deny"])
            x.add_argument("feedback", nargs="*")
            # One argument, so a client passing on someone else's text (the plugin, for Claude) cannot have
            # a word of it read as --force or --by: it sends --feedback=TEXT and puts `--` before ID.
            x.add_argument("--feedback", dest="feedback_text", help="feedback as one argument")
            x.add_argument("--force", action="store_true", help="human override of the risky-command tripwire")
            x.add_argument("--by", default="client", help="who decided, for the event log")
    for k, x in sub.choices.items():
        x.add_argument("-n", "--name", default="default")
        if k == "ask":
            x.add_argument("--image", action="append", default=[], help="attach an image file (repeatable)")
            x.add_argument("--skill", help="invoke this Muse skill (a selector from `skills`)")
            x.add_argument("--skill-args", help="free-text arguments for --skill, as one argument")
        if k == "interrupt":
            x.add_argument("--by", default="client", help="who interrupted, for the event log")
        if k in ("ask", "wait"):
            x.add_argument("--max-wait", type=int, default=540, help="seconds before returning with still_running")
    a = ap.parse_args()
    if a.cmd == "serve":
        serve(a)
    elif a.cmd == "decide":
        call(a.name, {"op": "decide", "id": a.id, "allow": a.verdict == "allow", "force": a.force, "by": a.by,
                      "feedback": a.feedback_text or " ".join(a.feedback) or None})
    elif a.cmd == "ask":
        call(a.name, {"op": "ask", "text": a.text, "maxWait": a.max_wait, "skill": a.skill, "skillArgs": a.skill_args,
                      "images": [os.path.abspath(i) for i in a.image]})
    elif a.cmd == "steer":
        call(a.name, {"op": "steer", "text": a.text})
    elif a.cmd == "wait":
        call(a.name, {"op": "wait", "maxWait": a.max_wait})
    elif a.cmd == "interrupt":
        call(a.name, {"op": "interrupt", "by": a.by})
    else:
        call(a.name, {"op": a.cmd})


if __name__ == "__main__":
    main()
