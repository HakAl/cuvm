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
def _pw():
    """The guest login password, from the host environment like the scripts read it."""
    return os.environ.get("CUVM_GUEST_PW", "admin")


def _env(gateway, port):
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
        cmd = "%s exec claude %s" % (_env(self.gw, self.port), " ".join(shlex.quote(a) for a in args))
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

    def mcp_check(self):
        self.keys("Enter", literal="/mcp")
        text, ok = "", False
        for _ in range(15):
            time.sleep(1)
            text = self.pane()
            if "computer-use" in text:
                ok = any("computer-use" in l and "✔" in l for l in text.splitlines())
                break
        self.keys("Escape")
        time.sleep(1)
        return ok, text

    def reach_check(self, timeout=60):
        cmd = ("%s claude -p 'Reply with exactly PONG' --tools '' --max-turns 1 --no-session-persistence "
               "< /dev/null > ~/reach.txt 2>&1" % _env(self.gw, self.port))
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

    def watch(self, cfg, done_file, deadline_s, on_dialog):
        """Poll until done, deadline or a blocked dialog. Returns end_reason."""
        end = time.monotonic() + deadline_s
        answered = 0
        while time.monotonic() < end:
            p = self.pane()
            d = dialog.parse(p)
            if d is not None:
                ok, why = dialog.decide(d, cfg["apps"], cfg["grants"], cfg["allow_fullscreen"])
                if ok and answered < 5:
                    approved = self._approve(d)
                    on_dialog(d, approved, why if approved else "allow option could not be selected")
                    answered += 1
                    if not approved:
                        return "blocked_on_approval"
                    time.sleep(2)
                    continue
                on_dialog(d, False, why)
                return "blocked_on_approval"
            if self.g.ok("test -s %s" % done_file) and "esc to interrupt" not in p:
                time.sleep(3)
                p2 = self.pane()
                if "esc to interrupt" not in p2 and dialog.parse(p2) is None:
                    return "completed"
            time.sleep(2)
        return "deadline"

    def _approve(self, d):
        """Move the cursor to the allow option, confirm it is selected, then Enter."""
        target = dialog.ALLOW_ACCESS if d.kind == "access" else "Allow"
        for _ in range(4):
            block = dialog.parse(self.pane())
            if block is None:
                return False
            sel = [l.strip() for l in block.text.splitlines() if l.strip().startswith("❯")]
            if sel and target in sel[0]:
                self.keys("Enter")
                return True
            self.keys("Down")
        return False
