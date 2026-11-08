"""The agent session in the guest's tmux: start, ready, reach check, prompt, watch."""
import os
import shlex
import datetime
import subprocess
import sys
import termios
import threading
import time

from cuvm import SCRIPTS, dialog

SESS = "cu"
MAX_DENIALS = 3
GRACE_S = 20   # after a denial or the reprompt, before idle is judged
IDLE_S = 6     # idle must hold this long with no work seen in between


def idle(pane):
    """The agent waits for input: no work in progress and the prompt box frame is drawn. The input
    line may hold a Claude Code prompt suggestion, so it is not required to be empty."""
    if "esc to interrupt" in pane:
        return False
    lines = pane.splitlines()
    return any(lines[i].startswith("❯") and lines[i - 1].startswith("────") and lines[i + 1].startswith("────")
               for i in range(1, len(lines) - 1))


def _same_but_cursor(a, b):
    """Two dialog texts are the same dialog if they differ only in where the cursor is."""
    return a.replace("❯", " ") == b.replace("❯", " ")
def _pw():
    """The guest login password, from the host environment like the scripts read it."""
    return os.environ.get("CUVM_GUEST_PW", "admin")


def env(gateway, port):
    px = "http://%s:%d" % (gateway, port)
    # Prefix assignments run after /etc/zshenv, so they win over its fixed port.
    return ("HTTPS_PROXY=%s HTTP_PROXY=%s NO_PROXY=localhost,127.0.0.1 NODE_EXTRA_CA_CERTS=$HOME/.cuvm/ca.pem"
            % (px, px))


class Session:
    def __init__(self, guest, gateway, port):
        self.g, self.gw, self.port = guest, gateway, port
        # attach runs on the main thread, release on the deadline's (Lookout round 5 R5-F2)
        self._lock = threading.Lock()
        self._attach_proc, self._released, self.stops = None, False, []

    def _script(self, name, *args, input=None):
        env = dict(os.environ, CUVM_KNOWN_HOSTS=str(self.g.known_hosts), CUVM_GUEST_USER=self.g.user)
        return subprocess.run([str(SCRIPTS / name), self.g.ip, *args], env=env, input=input,
                              capture_output=True, text=True, timeout=60)

    def inject(self, plan, token):
        """token is the run's dummy; it goes over stdin only."""
        return self._script("inject_login.sh", plan, input=token)

    def e3(self):
        """sha256 of the access token in the guest's stored login (never the token itself)."""
        return self.g.run(
            "security unlock-keychain -p %s ~/Library/Keychains/login.keychain-db >/dev/null 2>&1; "
            "security find-generic-password -s 'Claude Code-credentials' -w | python3 -c 'import hashlib,json,sys; "
            "print(hashlib.sha256(json.load(sys.stdin)[\"claudeAiOauth\"][\"accessToken\"].encode()).hexdigest())'; "
            "rc=$?; security lock-keychain ~/Library/Keychains/login.keychain-db; exit $rc" % shlex.quote(_pw()), timeout=30)

    def remove_login(self):
        return self._script("remove_login.sh")

    def e2(self):
        return self._script("e2_check.sh")

    def start(self, args):
        cmd = "%s exec claude %s" % (env(self.gw, self.port), " ".join(shlex.quote(a) for a in args))
        return self.g.run("security unlock-keychain -p %s ~/Library/Keychains/login.keychain-db && "
                          "tmux new -d -s %s -c ~/exp %s" % (shlex.quote(_pw()), SESS, shlex.quote(cmd)), timeout=30)

    def pane(self, history=False):
        r = self.g.run("tmux capture-pane -p %s -t %s:0" % ("-S -" if history else "", SESS), timeout=20)
        return r.stdout

    def keys(self, *keys, literal=None):
        if literal is not None:
            self.g.run("tmux send-keys -t %s:0 -l %s" % (SESS, shlex.quote(literal)), timeout=20)
            time.sleep(0.5)
        for k in keys:
            self.g.run("tmux send-keys -t %s:0 %s" % (SESS, k), timeout=20)
            time.sleep(0.3)

    def wait_ready(self, timeout=60):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            p = self.pane()
            if "Claude Code" in p and "❯" in p:
                return True, p
            time.sleep(1)
        return False, self.pane()

    def mcp_check(self, names=("computer-use",)):
        """{server: connected} after /mcp shows every one of names with ✔, or after 15 s."""
        self.keys("Enter", literal="/mcp")
        text, st = "", {n: False for n in names}
        for _ in range(15):
            time.sleep(1)
            text = self.pane()
            st = {n: any(n in l and "✔" in l for l in text.splitlines()) for n in names}
            if all(st.values()):
                break
        self.keys("Escape")
        time.sleep(1)
        return st, text

    def reach_check(self, timeout=60):
        cmd = ("%s claude -p 'Reply with exactly PONG' --tools '' --max-turns 1 --no-session-persistence "
               "< /dev/null > ~/reach.txt 2>&1" % env(self.gw, self.port))
        self.g.run("rm -f ~/reach.txt; tmux new-window -d -t %s -n reach %s" % (SESS, shlex.quote(cmd)), timeout=20)
        ok = self.g.wait(lambda: self.g.ok("grep -qx PONG ~/reach.txt"), timeout)
        out = self.g.run("cat ~/reach.txt; rm -f ~/reach.txt; tmux kill-window -t %s:reach 2>/dev/null; true" % SESS).stdout
        return ok, out

    # interactive mode (README, Interactive sessions)
    def log_pane(self):
        """Append everything the pane shows to ~/.cuvm/pane.log; it outlives the session."""
        return self.g.run("mkdir -p ~/.cuvm && tmux pipe-pane -o -t %s:0 'cat >> ~/.cuvm/pane.log'" % SESS, timeout=20)

    def attach(self):
        """Operator's terminal on the pane until detach, Claude exit or ssh drop. The terminal's
        settings are restored even if cuvm is interrupted (ssh killed cannot restore them). ssh is
        quiet and a newline follows, so tmux's last line is not run into cuvm's next one. Returns
        ssh's exit code, or None at once after release()."""
        fd = saved = None
        if sys.stdin.isatty():
            fd = sys.stdin.fileno()
            saved = termios.tcgetattr(fd)
        proc = None
        try:
            with self._lock:
                if self._released:
                    return None
                proc = self._attach_proc = subprocess.Popen(
                    ["ssh", "-t", "-q", *self.g.opts(), "-o", "ServerAliveInterval=30",
                     "%s@%s" % (self.g.user, self.g.ip), "tmux attach -t %s" % SESS])
            return proc.wait()
        except BaseException:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()
            raise
        finally:
            with self._lock:
                self._attach_proc = None
            if saved is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            sys.stdout.write("\n")
            sys.stdout.flush()

    def alive(self):
        """True: session running. False: gone (Claude exited or it was ended). None: guest unreachable."""
        try:
            rc = self.g.run("tmux has-session -t %s" % SESS, timeout=20).returncode
        except subprocess.TimeoutExpired:
            return None
        return True if rc == 0 else False if rc == 1 else None

    def end(self):
        """Kill the guest session. Never raises; every attempt is kept in stops, failures included."""
        rec = {"at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")}
        try:
            res = self.g.run("tmux kill-session -t %s 2>/dev/null; true" % SESS, timeout=20)
            rec["rc"] = res.returncode
            if res.returncode != 0:
                rec["stderr"] = (res.stderr or "").strip()[-200:]
        except subprocess.TimeoutExpired:
            rec.update(rc=None, error="timed out")
        self.stops.append(rec)
        return rec

    def release(self, grace=2.0):
        """End the host's attach ssh whatever happened in the guest, so the run reaches teardown
        (the deadline's backstop when end() could not reach the guest). Later attaches return at once."""
        with self._lock:
            self._released = True
            proc = self._attach_proc
        if proc is None or proc.poll() is not None:
            return "not_attached"
        try:
            proc.wait(timeout=grace)  # a successful end() lets ssh exit by itself
            return "exited"
        except subprocess.TimeoutExpired:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
            return "terminated"
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return "killed"

    def warn(self, msg):
        return self.g.run("tmux display-message -d 15000 -t %s %s" % (SESS, shlex.quote(msg)), timeout=20)

    def deliver(self, prompt):
        self.keys("Enter", literal=prompt)

    def watch(self, cfg, done_file, deadline_s, on_dialog, reprompt=None, on_reprompt=None):
        """Poll until done, deadline or a dialog left open. Returns end_reason.

        on_dialog(d, approved, why, action), action approved | denied | left_open. With
        cfg["on_refusal"] == "deny" a fully recognized access dialog that asks for apps or grants
        outside the config is denied (at most MAX_DENIALS) and watching goes on; once anything was
        denied, an idle agent gets `reprompt` once (if given), then ends idle_after_denial
        (deny-and-continue design; the post-deny pane was observed live)."""
        end = time.monotonic() + deadline_s
        deny = cfg.get("on_refusal", "stop") == "deny"
        answered = denials = 0
        armed, grace_until, idle_since, reprompted = False, 0.0, None, False
        while time.monotonic() < end:
            p = self.pane()
            d = dialog.parse(p)
            if d is not None:
                code, why = dialog.classify(d, cfg["apps"], cfg["grants"], cfg["allow_fullscreen"])
                if code == "ok" and answered < 5:
                    res = self._approve(d)
                    answered += 1
                    if res != "approved":
                        on_dialog(d, False, res, "left_open")
                        return "blocked_on_approval"
                    on_dialog(d, True, why, "approved")
                    time.sleep(2)
                    continue
                if code == "outside_config" and deny and denials < MAX_DENIALS:
                    res = self._deny(d)
                    if res != "denied":
                        on_dialog(d, False, res, "left_open")
                        return "blocked_on_approval"
                    on_dialog(d, False, why, "denied")
                    denials += 1
                    armed, grace_until, idle_since = True, time.monotonic() + GRACE_S, None
                    continue
                if code == "outside_config" and deny:
                    why += " (denial cap %d reached)" % MAX_DENIALS
                on_dialog(d, False, why, "left_open")
                return "blocked_on_approval"
            if self.g.ok("test -s %s" % done_file) and "esc to interrupt" not in p:
                time.sleep(3)
                p2 = self.pane()
                if "esc to interrupt" not in p2 and dialog.parse(p2) is None:
                    return "completed"
            if armed and time.monotonic() >= grace_until:
                if not idle(p):
                    idle_since = None
                elif idle_since is None:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since >= IDLE_S:
                    if reprompt and not reprompted:
                        self.deliver(reprompt)
                        reprompted = True
                        if on_reprompt:
                            on_reprompt(reprompt)
                        grace_until, idle_since = time.monotonic() + GRACE_S, None
                    else:
                        return "idle_after_denial"
            time.sleep(2)
        return "deadline"

    def _select(self, d, matches, move):
        """Move the cursor until the selected option matches, Enter, then confirm the dialog went
        away (or became a different one). Returns done | not_selectable | still_open."""
        for _ in range(4):
            block = dialog.parse(self.pane())
            if block is None or block.text != d.text and not _same_but_cursor(block.text, d.text):
                return "not_selectable"
            sel = [l.strip() for l in block.text.splitlines() if l.strip().startswith("❯")]
            if sel and matches(sel[0]):
                self.keys("Enter")
                for _ in range(3):
                    time.sleep(1)
                    after = dialog.parse(self.pane())
                    if after is None or not _same_but_cursor(after.text, d.text):
                        return "done"
                return "still_open"
            self.keys(move)
        return "not_selectable"

    def _approve(self, d):
        target = dialog.ALLOW_ACCESS if d.kind == "access" else "Allow"
        res = self._select(d, lambda line: target in line, "Down")
        return {"done": "approved", "not_selectable": "allow option could not be selected",
                "still_open": "dialog still open after allow"}[res]

    def _deny(self, d):
        res = self._select(d, lambda line: bool(dialog.DENY.match(line)), "Up")
        return {"done": "denied", "not_selectable": "deny option could not be selected",
                "still_open": "dialog still open after deny"}[res]
