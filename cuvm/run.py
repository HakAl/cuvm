"""One experiment run, end to end. Teardown always runs."""
import datetime
import fnmatch
import fcntl
import json
import os
import pathlib
import secrets
import signal
import socket
import subprocess
import time

from cuvm import PKG, config as cfgmod, image as imgmod, proxy, probes, transcript
from cuvm.guest import Guest
from cuvm.image import sha256
from cuvm.session import Session


def now():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sh(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


class Run:
    def __init__(self, config_path, out="runs", images="images", name=None, keep_on_fail=False):
        self.cfg, self.cfg_hash = cfgmod.load(config_path)
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
        self.t0 = time.monotonic()
        self.r = {"run_id": self.id, "started_at": now(), "config": self.config_path, "config_sha256": self.cfg_hash,
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
                          "tinyproxy": sh(["tinyproxy", "-v"]).stdout.strip()}

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
        self.proxy_log = self.rec_dir / "tinyproxy.log"
        conf = self.rec_dir / "tinyproxy.conf"
        for _ in range(3):  # a concurrent run may take the port between probe and bind
            self.port = proxy.free_port(self.gw)
            conf.write_text(proxy.render(self.gw, self.ip, self.port, self.cfg["net_allow"], str(self.proxy_log)))
            self.proxy_proc = proxy.start(conf, self.rec_dir / "tinyproxy.stdout")
            time.sleep(1)
            if self.proxy_proc.poll() is None:
                break
        else:
            raise RuntimeError("tinyproxy exited")
        self.r["net"].update(proxy_port=self.port, proxy_pid=self.proxy_proc.pid,
                             allow=pathlib.Path(self.cfg["net_allow"]).read_text().split())
        self.mark("t_proxy", port=self.port)

    def connect(self):
        self.guest = Guest(self.ip, self.image["guest_user"], self.out / "known_hosts")
        if not self.guest.wait_ssh(180):
            raise RuntimeError("ssh not up in 180 s")
        fp = self.guest.host_key_fp()
        self.r["guest"] = {"ssh_host_key_fp": fp}
        pinned = imgmod.pin_fp(self.image_path, fp) if fp else None
        if fp is None or fp != pinned:
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
        self.r["auth"] = {"method": "injected-login-keychain", "plan": self.cfg["plan"], "attempted_at": now()}
        inj = self.session.inject(self.cfg["plan"])
        self.save("inject.txt", inj.stdout + inj.stderr)
        if inj.returncode != 0:
            raise RuntimeError("login injection failed")
        self.r["auth"]["injected_at"] = now()
        self.mark("t_inject")
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

    def collect(self):
        self.save("pane-full.txt", self.session.pane(history=True))
        g = self.guest
        files = g.run("ls ~/.claude/projects/*/*.jsonl 2>/dev/null").stdout.split()
        marker, chosen = self.id, None
        for f in files:
            local = self.res_dir / "transcripts" / pathlib.Path(f).name
            g.pull(f, local)
            if local.exists() and marker in local.read_text(errors="replace"):
                chosen = local
        done = self.cfg["done"]["guest_file"]
        if g.ok("test -e %s" % done):
            g.pull(done, self.res_dir / pathlib.Path(done).name)
        g.run("cd ~/exp && tar czf ~/results.tgz results 2>/dev/null; true")
        g.pull("results.tgz", self.res_dir / "results.tgz")
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
            except Exception as e:  # keep tearing down
                self.fail("post-run step error: %s" % e)
        # Never keep a stopped clone that may still hold the login in its keychain.
        creds_gone = "attempted_at" not in self.r.get("auth", {}) or t.get("login_removed") is True
        keep = bool(self.keep_on_fail and self.r["fail_reasons"] and creds_gone)
        # Each host step runs even if an earlier one raised.
        for name, step in (("stop", self._stop_vm), ("delete", lambda: keep or sh(["tart", "delete", self.vm], timeout=120)),
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

    def _stop_proxy(self):
        if self.proxy_proc is not None:
            self.proxy_proc.terminate()
            try:
                self.proxy_proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proxy_proc.kill()

    def leaks(self, kept):
        tl = sh(["tart", "list"]).stdout
        pg = sh(["pgrep", "-fl", "tart run %s" % self.vm]).stdout.strip()
        proxy_alive = self.proxy_proc is not None and self.proxy_proc.poll() is None
        inputs = pathlib.Path(self.cfg["inputs"])
        after = {p.name: sha256(p) for p in sorted(inputs.iterdir()) if p.is_file()}
        gold = imgmod.hashes(self.cfg["image"])
        L = {"vm_listed": self.vm in tl, "tart_run_proc": pg, "proxy_alive": proxy_alive,
             "inputs_unchanged": after == self.r.get("inputs_sha256_before"),
             "golden_unchanged": gold == self.image["image_sha256"], "golden_sha256_after": gold}
        self.r["leaks"] = L
        if (L["vm_listed"] and not kept) or pg or proxy_alive or not L["inputs_unchanged"] or not L["golden_unchanged"]:
            self.fail("leak check: %s" % {k: v for k, v in L.items() if k != "golden_sha256_after"})

    def execute(self):
        self.ip = None

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
                self.experiment()
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
            self.teardown()
            if self.proxy_log.exists() if hasattr(self, "proxy_log") else False:
                self.r["proxy"] = px = proxy.parse_log(self.proxy_log.read_text())
                allow = self.r["net"]["allow"]
                outside = [h for h in px["allowed"] if not any(fnmatch.fnmatch(h, a) for a in allow)]
                if outside:
                    self.fail("proxy forwarded hosts outside net_allow: %s" % outside)
                if px["unauthorized"]:
                    self.fail("proxy saw unauthorized clients: %s" % px["unauthorized"])
            self.r["verdict"] = "FAIL" if self.r["fail_reasons"] else "PASS"
            self.r["finished_at"] = now()
            self.save("record.json", self.r)
            signal.signal(signal.SIGTERM, prev)
            signal.signal(signal.SIGINT, prev_int)
            with open(self.out / "runs.jsonl", "a") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.write(json.dumps({k: v for k, v in self.r.items() if k != "dialogs"} |
                                   {"dialogs": [{k: v for k, v in d.items() if k != "text"} for d in self.r["dialogs"]]}) + "\n")
            self.log("verdict %s %s" % (self.r["verdict"], self.r["fail_reasons"]))
        return self.r["verdict"] == "PASS"
