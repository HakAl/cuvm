"""One experiment run, end to end. Teardown always runs."""
import datetime
import fnmatch
import fcntl
import hashlib
import json
import os
import pathlib
import secrets
import select
import signal
import socket
import subprocess
import sys
import termios
import time

from cuvm import PKG, config as cfgmod, egress, image as imgmod, interactive as imode, probes, transcript
from cuvm.guest import Guest
from cuvm.image import sha256
from cuvm.session import Session

# Exit 0 with no output only when absence is established: ~/.claude or ~/.claude/projects does not
# exist (no entry, not even a link) inside a parent that is a searchable directory. A wrong object type,
# a parent that cannot be searched, or a find error exits nonzero (Lookout COMPUTERUSE-002 round 5
# R5-F1, round 6 R6-F1: a failed listing must not read as an empty session). The find follows links
# and does not filter on type, so every *.jsonl name it can see is listed even when it cannot be
# stat'ed; one that then cannot be pulled fails the run (agy R6-W1: BSD find -type f skips silently).
TRANSCRIPTS = ('h="$HOME"; c="$h/.claude"; d="$c/projects"; '
               'no() { echo "transcripts: $1" >&2; exit 2; }; '
               '{ [ -d "$h" ] && [ -x "$h" ]; } || no "home not a searchable directory"; '
               'if [ ! -e "$c" ] && [ ! -L "$c" ]; then exit 0; fi; '
               '{ [ -d "$c" ] && [ -x "$c" ]; } || no "~/.claude not a searchable directory"; '
               'if [ ! -e "$d" ] && [ ! -L "$d" ]; then exit 0; fi; '
               '[ -d "$d" ] || no "~/.claude/projects not a directory"; '
               'find -L "$d" -mindepth 2 -maxdepth 2 -name "*.jsonl"')


def now():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sh(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


class Run:
    def __init__(self, config_path, out="runs", images="images", name=None, keep_on_fail=False, interactive=False):
        self.mode = "interactive" if interactive else "scripted"
        self.cfg, self.cfg_hash = cfgmod.load(config_path, interactive=interactive)
        self.config_path = str(config_path)
        name = name or pathlib.Path(config_path).parent.name
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        self.id = "%s-%s-%s" % (name, stamp, secrets.token_hex(2))
        self.vm = "cuvm-" + self.id
        self.out = pathlib.Path(out).resolve()
        self.image_path, self.image = imgmod.load(images, self.cfg["image"])
        self.rec_dir = self.out / self.id
        self.res_dir = self.rec_dir / "results"
        self.res_dir.mkdir(parents=True)
        self.keep_on_fail = keep_on_fail
        # The guest's login holds only this; the egress proxy swaps in the real token on the host.
        self.dummy = "sk-ant-oat01-cuvm-dummy-" + secrets.token_hex(24)
        self.t0 = time.monotonic()
        self.r = {"run_id": self.id, "mode": self.mode, "started_at": now(), "config": self.config_path,
                  "config_sha256": self.cfg_hash,
                  "image": self.cfg["image"], "image_source": self.image.get("source"),
                  "timings": {}, "dialogs": [], "phases": [], "fail_reasons": []}
        self.tart_proc = self.proxy_proc = self.guest = self.session = None

    # bookkeeping
    def mark(self, phase, **extra):
        self.r["timings"][phase] = round(time.monotonic() - self.t0, 1)
        self.r["phases"].append(dict(phase=phase, at=now(), **extra))
        self.log("%s %s" % (phase, json.dumps(extra) if extra else ""))

    def log(self, msg):
        with open(self.rec_dir / "run.log", "a") as f:
            f.write("%s %s\n" % (now(), msg))
        print("[%s] %s" % (self.id, msg), flush=True)

    def fail(self, why):
        self.r["fail_reasons"].append(why)
        self.log("FAIL " + why)

    def save(self, name, text):
        (self.rec_dir / name).write_text(text if isinstance(text, str) else json.dumps(text, indent=1))

    # phases
    def preflight_host(self):
        self.log("checking golden image %s (reads the whole disk, about 25 s)" % self.cfg["image"])
        self.r["golden_sha256_before"] = gold = imgmod.hashes(self.cfg["image"])
        if gold != self.image["image_sha256"]:
            raise RuntimeError("golden hashes differ from %s (image changed since it was recorded)" % self.image_path)
        inputs = pathlib.Path(self.cfg["inputs"])
        self.r["inputs_sha256_before"] = {p.name: sha256(p) for p in sorted(inputs.iterdir()) if p.is_file()}
        ls = sh(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]).stdout.splitlines()[1:]
        wild = sorted({int(l.split()[8].rsplit(":", 1)[1]) for l in ls if l.split()[8].startswith("*:")})
        en0 = sh(["ipconfig", "getifaddr", "en0"]).stdout.strip()
        self.r["host"] = {"user": os.environ.get("USER"), "uid": os.getuid(), "hostname": socket.gethostname(),
                          "en0_addr": en0, "listeners_wildcard": wild,
                          "tart": sh(["tart", "--version"]).stdout.strip(),
                          "openssl": sh(["openssl", "version"]).stdout.strip()}

    def clone_boot(self):
        if sh(["tart", "clone", self.cfg["image"], self.vm]).returncode != 0:
            raise RuntimeError("tart clone failed")
        self.mark("t_clone")
        log = open(self.rec_dir / "tart-run.log", "w")
        self.tart_proc = subprocess.Popen(
            ["tart", "run", self.vm, "--net-host", "--dir", "inputs:%s:ro" % self.cfg["inputs"],
             "--no-clipboard", "--no-audio", "--no-usb-accessories"], stdout=log, stderr=subprocess.STDOUT)
        ip, end = "", time.monotonic() + 180
        while time.monotonic() < end and not ip:
            ip = sh(["tart", "ip", self.vm], timeout=20).stdout.strip()
            time.sleep(2)
        if not ip:
            raise RuntimeError("no guest ip in 180 s")
        self.ip, self.gw = ip, ip.rsplit(".", 1)[0] + ".1"
        self.r["net"] = {"guest_ip": ip, "gateway": self.gw}
        self.mark("t_ip", ip=ip)

    def start_proxy(self):
        """cuvm/egress.py as its own process: it reads the real token from the host keychain itself."""
        self.proxy_log = self.rec_dir / "egress.jsonl"
        self.ca_dir = self.rec_dir / "egress-ca"
        with open(self.rec_dir / "egress.stderr", "w") as err:
            self.proxy_proc = subprocess.Popen(
                [sys.executable, "-I", str(PKG / "egress.py"), "--listen", self.gw, "--guest", self.ip,
                 "--allow", self.cfg["net_allow"], "--dir", str(self.ca_dir), "--log", str(self.proxy_log)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True)
        self.proxy_proc.stdin.write(self.dummy + "\n")
        self.proxy_proc.stdin.close()
        ready = select.select([self.proxy_proc.stdout], [], [], 60)[0]
        line = self.proxy_proc.stdout.readline() if ready else ""
        if not line:
            raise RuntimeError("egress proxy did not start; see egress.stderr")
        self.port = json.loads(line)["port"]
        self.r["net"].update(proxy="egress", proxy_port=self.port, proxy_pid=self.proxy_proc.pid,
                             allow=pathlib.Path(self.cfg["net_allow"]).read_text().split())
        self.mark("t_proxy", port=self.port)

    def connect(self):
        self.guest = Guest(self.ip, self.image["guest_user"], self.out / "known_hosts")
        if not self.guest.wait_ssh(180):
            raise RuntimeError("ssh not up in 180 s")
        fp = self.guest.host_key_fp()
        self.r["guest"] = {"ssh_host_key_fp": fp}
        pinned, status = imgmod.pin_fp(self.image_path, fp) if fp else (None, "missing")
        self.r["guest"]["host_key_pin"] = status
        self.log("host key %s %s" % (status, fp))
        if status not in ("pinned", "matched"):
            raise RuntimeError("guest host key %s differs from image record %s" % (fp, pinned))
        want = self.image["settings_sha256"]
        got = self.guest.run("shasum -a 256 ~/.claude/settings.json", timeout=30).stdout.split()[:1]
        self.r["guest"]["settings_sha256"] = got[0] if got else None
        if got != [want]:
            raise RuntimeError("guest settings.json %s differs from baseline %s" % (got, want))
        self.mark("t_boot_to_ssh")
        # The clone's /etc/zshenv carries the image's fixed port; point every guest shell at this run's proxy.
        if not self.guest.ok("sudo sed -i '' 's/:8888\"/:%d\"/g' /etc/zshenv && grep -q ':%d\"' /etc/zshenv"
                             % (self.port, self.port)):
            raise RuntimeError("could not set the run's proxy port in /etc/zshenv")
        # Guest Node trusts the run's CA (images built before token isolation lack the zshenv line).
        ca = (self.ca_dir / "ca.pem").read_text()
        self.guest.run("mkdir -p ~/.cuvm && chmod 700 ~/.cuvm && cat > ~/.cuvm/ca.pem", input=ca, timeout=30)
        got = self.guest.run("shasum -a 256 ~/.cuvm/ca.pem", timeout=30).stdout.split()[:1]
        if got != [sha256(self.ca_dir / "ca.pem")]:
            raise RuntimeError("run CA not copied to the guest")
        if not self.guest.ok("grep -q NODE_EXTRA_CA_CERTS /etc/zshenv || "
                             "echo 'export NODE_EXTRA_CA_CERTS=\"$HOME/.cuvm/ca.pem\"' | sudo tee -a /etc/zshenv >/dev/null"):
            raise RuntimeError("could not add NODE_EXTRA_CA_CERTS to /etc/zshenv")
        ok, state = self.guest.fix_network(self.gw)
        self.save("network.txt", state)
        if not ok:
            raise RuntimeError("router and DNS not active within 30 s")
        self.mark("t_network")

    def run_probes(self, label):
        n3 = [(a, p) for a in (self.gw, self.r["host"]["en0_addr"]) for p in [22] + self.r["host"]["listeners_wildcard"]]
        cmds = probes.commands(self.gw, self.port, str(pathlib.Path.home()), str(PKG.parent), n3)
        before = self.proxy_log.read_text() if self.proxy_log.exists() else ""
        out = self.guest.run("zsh -s", input=probes.script(cmds, socket.gethostname().split(".")[0]), timeout=300).stdout
        time.sleep(1)
        after = self.proxy_log.read_text()[len(before):]
        known = sha256(pathlib.Path(self.cfg["inputs"]) / "known.txt")
        j = probes.judge(probes.parse(out), known, os.getuid(), socket.gethostname().split(".")[0], after, self.gw, self.port)
        e2 = self.session.e2() if self.session else Session(self.guest, self.gw, self.port).e2()
        j["E2"] = {"exit": e2.returncode, "out": e2.stdout, "pass": e2.returncode == 0, "why": ""}
        missing = [n for n, _ in cmds if n not in j]
        for n in missing:
            j[n] = {"exit": None, "out": "(no output)", "pass": False, "why": "probe did not report"}
        self.r.setdefault("probes", {})[label] = j
        bad = [n for n, v in j.items() if not v["pass"]]
        if bad:
            self.fail("%s probes failed: %s" % (label, bad))
        self.mark("t_" + label, failed=bad)
        return not bad

    def start_session(self):
        self.session = Session(self.guest, self.gw, self.port)
        # Set before injecting: a failed or interrupted inject may still have written the keychain.
        self.r["auth"] = {"method": "dummy-login-host-swap", "plan": self.cfg["plan"], "attempted_at": now()}
        inj = self.session.inject(self.cfg["plan"], self.dummy)
        self.save("inject.txt", inj.stdout + inj.stderr)
        if inj.returncode != 0:
            raise RuntimeError("login injection failed")
        self.r["auth"]["injected_at"] = now()
        self.mark("t_inject")
        # E3: the guest's stored login holds this run's dummy, not some other token.
        got = self.session.e3()
        want = hashlib.sha256(self.dummy.encode()).hexdigest()
        e3 = {"exit": got.returncode, "out": "match" if got.stdout.strip() == want else "differs",
              "pass": got.returncode == 0 and got.stdout.strip() == want, "why": "stored token sha256 vs the run's dummy"}
        self.r.setdefault("probes", {})["session"] = {"E3": e3}
        if not e3["pass"]:
            raise RuntimeError("E3: guest login is not the run's dummy")
        st = self.session.start(self.cfg["agent_args"])
        if st.returncode != 0:
            raise RuntimeError("session start failed: %s" % st.stderr.strip())
        ok, pane = self.session.wait_ready(60)
        self.save("pane-ready.txt", pane)
        if not ok:
            raise RuntimeError("agent prompt not ready in 60 s")
        ok, mcp = self.session.mcp_check()
        self.save("mcp-pane.txt", mcp)
        self.r["guest"]["mcp_computer_use"] = ok
        if not ok:
            raise RuntimeError("computer-use not enabled in /mcp")
        ok, out = self.session.reach_check(60)
        self.save("reach.txt", out)
        self.r["reach_check"] = {"pass": ok, "out": out.strip()[:300]}
        if not ok:
            raise RuntimeError("agent cannot reach the API from tmux (reach check)")
        self.mark("t_ready")

    def experiment(self):
        prompt = self.cfg["prompt"].replace("{run_id}", self.id)
        self.session.deliver(prompt)
        self.mark("t_deliver")

        def on_dialog(d, approved, why):
            self.r["dialogs"].append({"at": now(), "kind": d.kind, "apps": d.apps, "grants": d.grants, "extra": d.extra,
                                      "approved": approved, "why": why, "text": d.text})
            self.log("dialog %s apps=%s approved=%s (%s)" % (d.kind, d.apps, approved, why))

        done = self.cfg["done"]["guest_file"]
        self.r["end_reason"] = self.session.watch(self.cfg, done, self.cfg["deadline_s"], on_dialog)
        self.mark("t_run", end_reason=self.r["end_reason"])
        if self.r["end_reason"] != "completed":
            self.fail("end_reason " + self.r["end_reason"])

    def interactive(self):
        """The operator drives the session; it ends when Claude exits (README, Interactive sessions)."""
        s = self.session
        s.log_pane()
        ignored = [k for k in cfgmod.SCRIPTED_ONLY if k in self.cfg]
        if ignored:
            self.log("interactive: config keys %s ignored" % ignored)
        lim = self.cfg["deadline_s"]
        self.mark("t_attach")
        print("\n[%s] guest %s. Exit Claude (/exit) to end the run; Ctrl-b d detaches. Time limit %d min.\n"
              % (self.id, self.ip, lim // 60), flush=True)
        obs = imode.Observer(s, self.r["dialogs"].append)
        def stop():  # Lookout round 5 R5-F2: the host lets go even if the guest stop fails
            try:
                s.end()
            finally:
                s.release()
        dl = imode.Deadline(lim, warn=lambda: s.warn("cuvm: 5 minutes left, then the run ends"), end=stop)
        obs.start()
        dl.start()
        try:
            self.r["end_reason"] = imode.drive(s, dl.fired, imode.ask)
        finally:
            dl.cancel()
            obs.stop()
            self.r["session_stops"] = list(s.stops)
            for e in dl.errors:
                self.log("deadline callback failed: %s" % e)
        for st in self.r["session_stops"]:
            if st.get("rc") != 0:
                self.log("guest session stop failed %s; teardown stops the VM" % json.dumps(st))
        self.mark("t_run", end_reason=self.r["end_reason"])
        if self.r["end_reason"] == "guest_unreachable":
            self.fail("guest stopped answering during the interactive session")

    def transcripts(self):
        """Guest transcript paths, sorted; None, with a fail reason, when they could not be listed."""
        try:
            res = self.guest.run(TRANSCRIPTS, timeout=60)
        except subprocess.TimeoutExpired:
            self.fail("transcript listing failed: timed out")
            return None
        if res.returncode != 0:
            self.fail("transcript listing failed: exit %d %s" % (res.returncode, res.stderr.strip()[-200:]))
            return None
        return sorted(res.stdout.split())

    def archive_results(self):
        """results.tgz of the results directory; any tar or copy failure fails the run, in both modes
        (round 5 R5-F3: no empty-archive fallback)."""
        tar = self.guest.run("tar czf ~/results.tgz -C %s ." % self.cfg["results"])  # path checked by config.load
        if tar.returncode != 0:
            self.fail("results archive of %s failed: %s" % (self.cfg["results"], tar.stderr.strip()[-200:]))
        elif not self.guest.pull("results.tgz", self.res_dir / "results.tgz"):
            self.fail("results archive could not be pulled")

    def collect_interactive(self):
        g = self.guest
        if not g.pull("~/.cuvm/pane.log", self.rec_dir / "pane.log"):
            self.log("pane.log could not be pulled")
        files = self.transcripts() or []
        a = {"transcript_files": [], "tools_used": [], "forbidden_tools_attempted": [], "forbidden_tools_used": [],
             "request_access_granted": []}
        for f in files:
            local = self.res_dir / "transcripts" / pathlib.Path(f).name
            if not g.pull(f, local) or not local.exists():
                self.fail("transcript %s could not be pulled" % f)
                continue
            a["transcript_files"].append(str(local.relative_to(self.rec_dir)))
            t = transcript.extract(local.read_text().splitlines(), ("mcp__computer-use__",), ("ToolSearch",))
            for k in ("tools_used", "forbidden_tools_attempted", "forbidden_tools_used", "request_access_granted"):
                a[k] = sorted(set(a[k]) | set(t[k]))
        a["granted_outside_config"] = sorted(set(a["request_access_granted"]) - set(self.cfg["apps"]))
        a["pane_log"] = "pane.log" if (self.rec_dir / "pane.log").exists() else None
        self.archive_results()
        self.mark("t_collect")
        if a["forbidden_tools_used"]:
            self.fail("forbidden tools ran: %s" % a["forbidden_tools_used"])
        if a["forbidden_tools_attempted"]:
            self.log("agent attempted forbidden tools %s; refused by Claude Code" % a["forbidden_tools_attempted"])
        if a["granted_outside_config"]:
            self.log("operator granted apps outside the config: %s" % a["granted_outside_config"])
        self.r["artefacts"] = a

    def collect(self):
        if self.mode == "interactive":
            return self.collect_interactive()
        self.save("pane-full.txt", self.session.pane(history=True))
        g = self.guest
        files = self.transcripts() or []
        marker, chosen = self.id, None
        for f in files:
            local = self.res_dir / "transcripts" / pathlib.Path(f).name
            g.pull(f, local)
            if local.exists() and marker in local.read_text(errors="replace"):
                chosen = local
        done = self.cfg["done"]["guest_file"]
        if g.ok("test -e %s" % done):
            g.pull(done, self.res_dir / pathlib.Path(done).name)
        self.archive_results()
        self.mark("t_collect")
        a = {"transcript_file": str(chosen.relative_to(self.rec_dir)) if chosen else None}
        if chosen:
            t = transcript.extract(chosen.read_text().splitlines(), ("mcp__computer-use__",), ("ToolSearch",))
            img = t.pop("final_image_bytes")
            if img:
                ext = "jpg" if t["final_image_media_type"] == "image/jpeg" else "png"
                (self.res_dir / ("screenshot-final." + ext)).write_bytes(img)
                a["screenshot_final"] = str((self.res_dir / ("screenshot-final." + ext)).relative_to(self.rec_dir))
            a.update(t)
            if t["forbidden_tools_used"]:
                self.fail("forbidden tools ran: %s" % t["forbidden_tools_used"])
            if t["forbidden_tools_attempted"]:
                self.log("agent attempted forbidden tools %s; refused by Claude Code" % t["forbidden_tools_attempted"])
            if not t["request_access_granted"] or set(t["request_access_granted"]) - set(self.cfg["apps"]):
                self.fail("granted apps %s not a non-empty subset of config" % t["request_access_granted"])
            if not t["input_tool_ok_before_final_shot"]:
                self.fail("no successful type/key before the final image")
            if t["final_image_is_error"] is not False:
                self.fail("final image missing or from an error result")
        else:
            self.fail("no transcript contains the run id")
        a["screenshot_shows_task"] = None  # human review; not automated
        checks = []
        for i, c in enumerate(self.cfg["checks"]):
            path, want = c["guest_file_equals"]
            local = self.res_dir / "checks" / ("%d-%s" % (i, pathlib.Path(path).name))
            g.pull(path, local)
            got = local.read_text().strip() if local.exists() else None
            want = want.replace("{run_id}", self.id)
            checks.append({"file": path, "want": want, "got": got, "pass": got == want})
            if got != want:
                self.fail("check %s: got %r" % (path, got))
        a["checks"] = checks
        self.r["artefacts"] = a

    def teardown(self):
        t = {}
        if self.guest is not None and self.ip:
            try:
                self.guest.run("tmux kill-server 2>/dev/null; true", timeout=20)
                rm = Session(self.guest, self.gw, getattr(self, "port", 0)).remove_login()
                self.save("remove-login.txt", rm.stdout + rm.stderr)
                t["login_removed"] = rm.returncode == 0
                self.r["auth"] = dict(self.r.get("auth", {}), removed_at=now())
                if not t["login_removed"]:
                    self.fail("login removal or post E2 failed")
                if getattr(self, "port", None) and self.r.get("probes", {}).get("pre"):
                    self.run_probes("post")
                # The CA certificate is public (its key stays on the host); removed for kept clones.
                self.r.setdefault("guest", {})["ca_removed"] = self.guest.ok("rm -rf ~/.cuvm && test ! -e ~/.cuvm")
            except Exception as e:  # keep tearing down
                self.fail("post-run step error: %s" % e)
        # Never keep a stopped clone that may still hold the login in its keychain.
        creds_gone = "attempted_at" not in self.r.get("auth", {}) or t.get("login_removed") is True
        keep = bool(self.keep_on_fail and self.r["fail_reasons"] and creds_gone)
        # Each host step runs even if an earlier one raised.
        for name, step in (("stop", self._stop_vm), ("delete", lambda: keep or self._delete_vm()),
                           ("proxy", self._stop_proxy)):
            try:
                step()
            except Exception as e:
                self.fail("teardown %s error: %s" % (name, e))
        self.mark("t_teardown", kept_vm=bool(keep))
        self.leaks(keep)

    def _stop_vm(self):
        if self.tart_proc is None:
            return
        try:
            sh(["tart", "stop", self.vm, "--timeout", "30"], timeout=90)
        finally:
            try:
                self.tart_proc.wait(30)
            except subprocess.TimeoutExpired:
                self.tart_proc.kill()
                self.tart_proc.wait(10)

    def _delete_vm(self):
        d = sh(["tart", "delete", self.vm], timeout=120)
        if d.returncode != 0:
            raise RuntimeError("tart delete exit %d: %s" % (d.returncode, d.stderr.strip()))

    def _stop_proxy(self):
        if self.proxy_proc is not None:
            self.proxy_proc.terminate()
            try:
                self.proxy_proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proxy_proc.kill()

    def leaks(self, kept):
        """Each observation is True/False, or None when it could not be made; None fails the run."""
        L, errors = {}, []

        def observe(key, fn):
            try:
                L[key] = fn()
            except Exception as e:
                L[key] = None
                errors.append("%s: %s" % (key, e))

        def listed():
            tl = sh(["tart", "list"])
            if tl.returncode != 0:
                raise RuntimeError("tart list exit %d" % tl.returncode)
            return self.vm in tl.stdout

        def tart_proc():
            pg = sh(["pgrep", "-fl", "tart run %s" % self.vm])
            if pg.returncode not in (0, 1):  # 1 is pgrep's no-match
                raise RuntimeError("pgrep exit %d" % pg.returncode)
            return pg.stdout.strip()

        def inputs():
            d = pathlib.Path(self.cfg["inputs"])
            return {p.name: sha256(p) for p in sorted(d.iterdir()) if p.is_file()} == self.r.get("inputs_sha256_before")

        def golden():
            self.log("rechecking golden image (about 25 s)")
            L["golden_sha256_after"] = gold = imgmod.hashes(self.cfg["image"])
            return gold == self.image["image_sha256"]
        observe("vm_listed", listed)
        observe("tart_run_proc", tart_proc)
        observe("proxy_alive", lambda: self.proxy_proc is not None and self.proxy_proc.poll() is None)
        observe("inputs_unchanged", inputs)
        observe("golden_unchanged", golden)
        L["errors"] = errors
        self.r["leaks"] = L
        bad = (errors or (L["vm_listed"] and not kept) or L["tart_run_proc"] or L["proxy_alive"]
               or L["inputs_unchanged"] is False or L["golden_unchanged"] is False)
        if bad:
            self.fail("leak check: %s" % {k: v for k, v in L.items() if k != "golden_sha256_after"})

    def check_proxy_log(self):
        if not (hasattr(self, "proxy_log") and self.proxy_log.exists()):
            return
        self.r["proxy"] = px = egress.summarize(self.proxy_log.read_text())
        allow = self.r["net"]["allow"]
        outside = [h for h in px["allowed"] if not any(fnmatch.fnmatch(h, a) for a in allow)]
        if outside:
            self.fail("proxy forwarded hosts outside net_allow: %s" % outside)
        if px["unauthorized"]:
            self.fail("proxy saw unauthorized clients: %s" % px["unauthorized"])
        if px["real_in_response"]:
            self.fail("the real token appeared in %d upstream response(s); connections closed" % px["real_in_response"])
        if px["dummy_elsewhere"]:
            self.log("proxy refused %d request(s) carrying the dummy outside the auth header" % px["dummy_elsewhere"])
        if px["bad_response"] or px["surplus_request_bytes"]:
            self.log("proxy cut %d response(s) it could not scan and %d connection(s) carrying a second request"
                     % (px["bad_response"], px["surplus_request_bytes"]))

    def execute(self):
        self.ip = None
        tty = None
        if self.mode == "interactive" and sys.stdin.isatty():
            tty = termios.tcgetattr(sys.stdin.fileno())

        def on_term(signum, frame):
            raise KeyboardInterrupt("signal %d" % signum)
        prev = signal.signal(signal.SIGTERM, on_term)
        try:
            self.preflight_host()
            self.clone_boot()
            self.start_proxy()
            self.connect()
            if self.run_probes("pre"):
                self.start_session()
                self.interactive() if self.mode == "interactive" else self.experiment()
                self.collect()
        except Exception as e:
            self.r["end_reason"] = self.r.get("end_reason") or "error"
            self.fail("error: %s" % e)
        except KeyboardInterrupt as e:
            self.r["end_reason"] = "operator_interrupt"
            self.fail("interrupted: %s" % (e or "SIGINT"))
        finally:
            # No second interrupt may cut teardown or the record short.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            prev_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                try:
                    self.teardown()
                except Exception as e:  # an unobserved teardown is a FAIL, and the record is still written
                    self.fail("teardown error: %s" % e)
                try:
                    self.check_proxy_log()
                except Exception as e:
                    self.fail("proxy log error: %s" % e)
                self.r["verdict"] = "FAIL" if self.r["fail_reasons"] else "PASS"
                self.r["finished_at"] = now()
                self.save("record.json", self.r)
                with open(self.out / "runs.jsonl", "a") as f:
                    fcntl.flock(f, fcntl.LOCK_EX)
                    f.write(json.dumps({k: v for k, v in self.r.items() if k != "dialogs"} |
                                       {"dialogs": [{k: v for k, v in d.items() if k != "text"} for d in self.r["dialogs"]]}) + "\n")
            finally:
                signal.signal(signal.SIGTERM, prev)
                signal.signal(signal.SIGINT, prev_int)
                if tty is not None:  # whatever happened while attached, leave the terminal usable
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, tty)
            self.log("verdict %s %s" % (self.r["verdict"], self.r["fail_reasons"]))
        return self.r["verdict"] == "PASS"
