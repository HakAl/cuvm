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
import shlex
import signal
import socket
import subprocess
import sys
import termios
import time

from cuvm import PKG, browser, config as cfgmod, egress, image as imgmod, interactive as imode, probes, project, tools, transcript
from cuvm.guest import Guest
from cuvm.image import sha256
from cuvm.session import Session, env as session_env

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
        browser.check_image(self.cfg, self.image)
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
        self.collected = set()  # names of the collectors that completed (normal collection or salvage)
        self.project_before, self.home_bad_before = {}, 0  # the before manifest and listing's bad count, for a rerun

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
        if "tools" in self.cfg:
            self.install_tools()
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
        if "browser" in self.cfg:
            self.install_browser()
        ok, state = self.guest.fix_network(self.gw)
        self.save("network.txt", state)
        if not ok:
            raise RuntimeError("router and DNS not active within 30 s")
        self.mark("t_network")
        if "project" in self.cfg:
            self.unpack_project()
        if "dev_server" in self.cfg:
            self.start_dev_server()

    def install_tools(self):
        """Per-run settings in the clone allowing the config's `tools` (the guest-tools design). The image's
        file is verified first (connect), so this derives from the same text; the attempt is on record
        before anything can fail, and the hash read back is what the record keeps."""
        tl = list(self.cfg["tools"])
        self.r["tools"] = tl
        self.r["guest"]["settings_baseline_sha256"] = self.image["settings_sha256"]
        if sha256(tools.BASE) != self.image["settings_sha256"]:
            raise RuntimeError("image settings are not cuvm/data/guest-settings.json; cannot derive a per-run file")
        text = tools.settings(tl)
        self.r["guest"]["settings_sha256"] = None  # a failed or timed-out write leaves no stale hash
        w = self.guest.run("cat > ~/.claude/settings.json", input=text, timeout=30)
        if w.returncode != 0:
            raise RuntimeError("could not install the per-run settings: %s" % (w.stderr or "").strip()[-200:])
        got = self.guest.run("shasum -a 256 ~/.claude/settings.json", timeout=30).stdout.split()[:1]
        self.r["guest"]["settings_sha256"] = got[0] if got else None
        if got != [hashlib.sha256(text.encode()).hexdigest()]:
            raise RuntimeError("per-run settings differ after install: %s" % got)
        self.log("tools: settings installed allowing %s" % tl)

    # project snapshot and dev server (the web-dev design)
    def unpack_project(self):
        arc, top = self.cfg["project"], self.cfg["project_top"]
        live = "%s/%s" % (project.GUEST_DIR, top)
        t = time.monotonic()
        r = self.guest.run("rm -rf %s && mkdir -p %s && tar xzf '%s/%s' -C %s && test -d %s"
                           % (project.GUEST_DIR, project.GUEST_DIR, project.SHARE_INPUTS, arc, project.GUEST_DIR, live),
                           timeout=600)
        if r.returncode != 0:
            raise RuntimeError("project unpack failed: %s" % r.stderr.strip()[-200:])
        unpack_s = round(time.monotonic() - t, 1)
        m = self.guest.run(project.MANIFEST_CMD % project.GUEST_DIR, timeout=300)  # the root: siblings count (cuvm-vzi)
        if m.returncode != 0:
            raise RuntimeError("project manifest failed: %s" % m.stderr.strip()[-200:])
        try:
            manifest = project.parse_manifest(m.stdout)
        except project.ProjectError as e:
            raise RuntimeError("project manifest: %s" % e)
        self.r["project"] = {"archive": arc, "archive_sha256": self.r.get("inputs_sha256_before", {}).get(arc),
                             "top_dir": top, "dir": live, "unpack_s": unpack_s, "files": len(manifest), "manifest": manifest}
        self.mark("t_unpack", unpack_s=unpack_s, files=len(manifest))

    def dev_pane(self):
        """The dev tmux pane; if the server already exited (the pane is gone) the log it wrote."""
        return self.guest.run("tmux -L dev capture-pane -p -S - -t dev:0 2>/dev/null || cat %s/dev-server.log 2>/dev/null; true"
                              % self.cfg["results"], timeout=20).stdout

    def start_dev_server(self):
        """The configured command in its own tmux server (socket `dev`, so the agent's keychain-unlocked
        server is still created by Session.start), on guest loopback, ready before the pre-probes."""
        d, port = self.cfg["dev_server"], self.cfg["dev_server"]["port"]
        cwd = "%s/%s" % (project.GUEST_DIR, self.cfg["project_top"])
        log = "%s/dev-server.log" % self.cfg["results"]
        inner = "%s %s 2>&1 | tee %s" % (session_env(self.gw, self.port), d["command"], log)
        r = self.guest.run("mkdir -p %s && tmux -L dev new -d -s dev -c %s %s"
                           % (self.cfg["results"], cwd, shlex.quote(inner)), timeout=30)
        if r.returncode != 0:
            raise RuntimeError("dev server start failed: %s" % r.stderr.strip()[-200:])
        self.r["dev_server"] = {"command": d["command"], "port": port, "cwd": cwd, "log": log, "started_at": now(),
                                "ready": False, "listen": []}
        probe = "curl -s -o /dev/null -w '%%{http_code}' --noproxy '*' -m 5 http://127.0.0.1:%d/" % port
        ready = self.guest.wait(lambda: self.guest.run(probe, timeout=20).stdout.strip() == "200", d["ready_s"], every=1)
        self.r["dev_server"]["ready"] = ready
        if not ready:
            self.save("dev-server-pane.txt", self.dev_pane())
            raise RuntimeError("dev server not ready in %d s (no 200 from 127.0.0.1:%d)" % (d["ready_s"], port))
        ls = self.guest.run("lsof -nP -iTCP:%d -sTCP:LISTEN" % port, timeout=30)
        d1 = project.listen_judge(ls.stdout, ls.returncode, port)
        self.r.setdefault("probes", {}).setdefault("pre", {})["D1"] = d1
        self.r["dev_server"].update(ready_at=now(), listen=d1["listen"])
        if not d1["pass"]:
            self.save("dev-server-pane.txt", self.dev_pane())
            raise RuntimeError("D1: dev server must listen on loopback only (%s)" % d1["why"])
        self.mark("t_dev_server", listen=d1["listen"])

    def stop_dev_server(self):
        """Pane capture, then the dev tmux server; D2 over the session window of the egress log."""
        self.save("dev-server-pane.txt", self.dev_pane())
        self.guest.run("tmux -L dev kill-server 2>/dev/null; true", timeout=20)
        off = getattr(self, "egress_offset", None)
        if off is not None and self.proxy_log.exists():
            window = self.proxy_log.read_bytes()[off:].decode("utf-8", "replace")
            self.r.setdefault("probes", {}).setdefault("post", {})["D2"] = project.loopback_judge(window)

    def collect_project(self):
        """What changed under the project: manifest diff, a patch against a clean unpack, the changed files.
        A guest timeout here is a fail reason, not an exception: the results archive still follows (Lookout F1)."""
        p = self.r.get("project")
        if not p:
            return
        try:
            self._collect_project(p)
        except subprocess.TimeoutExpired as e:
            self.fail("project collection timed out: %s" % str(e)[:200])
            return True

    def _collect_project(self, p):
        """Manifest, diff and archive over the ~/exp/project root, not only <top>: a directory the agent
        creates beside the project is agent output too (bead cuvm-vzi, the guest-output design)."""
        g, arc = self.guest, p["archive"]
        if "manifest" in p:  # leaves the record here; kept for a rerun after an interrupt (agy fix round F1)
            self.project_before = p.pop("manifest")
        before = self.project_before
        # Every field is present (null) and the before-manifest is on disk before the first guest command
        # that can time out (Lookout R2-F3).
        p.update(added=None, modified=None, deleted=None, patch=None, changed_archive=None)
        self.save("project-manifest.json", {"before": before, "after": None})
        m = g.run(project.MANIFEST_CMD % project.GUEST_DIR, timeout=300)
        after = None
        if m.returncode != 0:
            self.fail("project manifest failed: exit %d %s" % (m.returncode, m.stderr.strip()[-200:]))
        else:
            try:
                after = project.parse_manifest(m.stdout)
            except project.ProjectError as e:
                self.fail("project manifest: %s" % e)
        if after is not None:
            p.update(project.classify(before, after))
            self.save("project-manifest.json", {"before": before, "after": after})
        u = g.run("rm -rf %s && mkdir -p %s && tar xzf '%s/%s' -C %s %s"
                  % (project.CLEAN_DIR, project.CLEAN_DIR, project.SHARE_INPUTS, arc, project.CLEAN_DIR, project.CLEAN_EXCLUDE),
                  timeout=600)
        if u.returncode != 0:
            self.fail("project clean unpack failed: %s" % u.stderr.strip()[-200:])
        else:
            d = g.run("diff -ruN --exclude node_modules %s %s > %s" % (project.CLEAN_DIR, project.GUEST_DIR, project.PATCH), timeout=300)
            if d.returncode not in (0, 1):
                self.fail("project diff failed: exit %d %s" % (d.returncode, d.stderr.strip()[-200:]))
            elif g.pull(project.PATCH, self.rec_dir / "project.patch"):
                p["patch"] = "project.patch"
            else:
                self.fail("project patch could not be pulled")
        changed = (p["added"] or []) + (p["modified"] or [])
        if changed:
            t = g.run("cat > %s && tar czf %s -C %s -T %s" % (project.LIST, project.CHANGED, project.GUEST_DIR, project.LIST),
                      input=project.list_input(changed), timeout=300)
            if t.returncode != 0:
                self.fail("project changed-files archive failed: %s" % t.stderr.strip()[-200:])
            elif g.pull(project.CHANGED, self.rec_dir / "project-changed.tgz"):
                p["changed_archive"] = "project-changed.tgz"
            else:
                self.fail("project changed-files archive could not be pulled")

    def install_browser(self):
        """The Playwright MCP config, before the pre-probes so B1 sees it (docs/records.md)."""
        path = browser.config_path(self.image)
        text = json.dumps(browser.mcp_config(self.cfg, self.image, self.gw, self.port), indent=1) + "\n"
        self.guest.run("mkdir -p ~/.cuvm && chmod 700 ~/.cuvm && cat > %s" % path, input=text, timeout=30)
        want = hashlib.sha256(text.encode()).hexdigest()
        got = self.guest.run("shasum -a 256 %s" % path, timeout=30).stdout.split()[:1]
        if got != [want]:
            raise RuntimeError("browser MCP config not written to the guest")
        b = self.image["browser"]
        self.r["browser"] = {"engine": self.cfg["browser"], "headless": bool(self.cfg.get("browser_headless")),
                             "image": {k: v for k, v in b.items()}, "mcp_config": json.loads(text),
                             "mcp_config_sha256": want}
        self.browser_config_sha = want

    def browser_probe(self):
        """B1: the MCP config and the server entry point are the ones written and recorded."""
        path, entry = browser.config_path(self.image), self.image["browser"]["entry"]
        res = self.guest.run("shasum -a 256 %s %s" % (path, entry), timeout=30)
        got = dict(reversed(l.split(None, 1)) for l in res.stdout.splitlines() if len(l.split(None, 1)) == 2)
        want = {path: self.browser_config_sha, entry: self.image["browser"]["entry_sha256"]}
        bad = [p for p, h in want.items() if got.get(p) != h]
        return {"exit": res.returncode, "out": res.stdout.strip()[-400:], "pass": res.returncode == 0 and not bad,
                "why": "sha256 of the MCP config and entry point vs the record" + (" (differ: %s)" % bad if bad else "")}

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
        if "browser" in self.cfg:
            j["B1"] = self.browser_probe()
        missing = [n for n, _ in cmds if n not in j]
        for n in missing:
            j[n] = {"exit": None, "out": "(no output)", "pass": False, "why": "probe did not report"}
        j = {**self.r.get("probes", {}).get(label, {}), **j}  # D1 (pre) and D2 (post) are recorded earlier
        self.r.setdefault("probes", {})[label] = j
        bad = [n for n, v in j.items() if not v["pass"]]
        if bad:
            self.fail("%s probes failed: %s" % (label, bad))
        self.mark("t_" + label, failed=bad)
        if label == "pre":  # D2 judges the egress log from here to the post-probes (the web-dev design)
            self.egress_offset = len(self.proxy_log.read_bytes()) if self.proxy_log.exists() else 0
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
        st = self.session.start(self.cfg["agent_args"] + tools.claude_args(self.cfg) + browser.claude_args(self.cfg, self.image))
        if st.returncode != 0:
            raise RuntimeError("session start failed: %s" % st.stderr.strip())
        ok, pane = self.session.wait_ready(60)
        self.save("pane-ready.txt", pane)
        if not ok:
            raise RuntimeError("agent prompt not ready in 60 s")
        names = ("computer-use", "playwright") if "browser" in self.cfg else ("computer-use",)
        st, mcp = self.session.mcp_check(names)
        self.save("mcp-pane.txt", mcp)
        self.r["guest"]["mcp_computer_use"] = st["computer-use"]
        if "browser" in self.cfg:
            self.r["guest"]["mcp_playwright"] = st["playwright"]
        down = [n for n in names if not st[n]]
        if down:
            raise RuntimeError("%s not connected in /mcp" % ", ".join(down))
        ok, out = self.session.reach_check(60)
        self.save("reach.txt", out)
        self.r["reach_check"] = {"pass": ok, "out": out.strip()[:300]}
        if not ok:
            raise RuntimeError("agent cannot reach the API from tmux (reach check)")
        self.home_before_listing()
        self.mark("t_ready")

    # home capture (the guest-output design): what the agent wrote anywhere under its home
    def home_before_listing(self):
        """The home listing after the last harness write and before the agent runs. A timeout or a non-zero
        exit is a fail reason that disables the capture (home_changes null); the run goes on."""
        self.r["home_changes"] = {"added": None, "modified": None, "deleted": None, "archive": None,
                                  "files": None, "bytes": None, "capped": None, "bad_names": 0}
        self.home_before = None
        try:
            l = self.guest.run(project.HOME_LIST_CMD % ("~", project.home_prunes(self.cfg)), timeout=300)
            why = None if l.returncode == 0 else "exit %d %s" % (l.returncode, l.stderr.strip()[-200:])
        except subprocess.TimeoutExpired as e:
            why = "timed out: %s" % str(e)[:200]
        if why:
            self.fail("home listing before the agent failed: %s" % why)
            self.r["home_changes"] = None
            return
        self.save("home-before.txt", l.stdout)
        self.home_before, self.home_bad_before = project.parse_listing(l.stdout)
        self.r["home_changes"]["bad_names"] = self.home_bad_before

    COLLECTORS = ("collect_project", "collect_home", "archive_results")

    def collect_outputs(self):
        """Everything the agent produced, in both modes: the project snapshot, the rest of the home, then the
        results directory. Each collector handles its own failures as fail reasons and returns True when the
        guest timed out, so the next one still runs; a collector is marked complete only when it returned,
        so salvage() after an interrupt runs the ones that did not (agy fix round F1). After a timeout the
        guest is pinged first and the rest is skipped with a fail reason when it no longer answers: a wedged
        guest costs one timeout, not every one in turn while signals are ignored (agy fix round F2). The ping
        also follows a timeout of the last collector (Lookout round 3 F1-R3). Returns whether the guest answered
        the last time it was asked, so the caller knows whether to ask it anything more; completion is a
        separate fact (`collected_all`). When collectors were skipped the record says `salvage:
        guest_unreachable`, whichever path was collecting, and the salvage does not try again."""
        stalled = False
        for name in self.COLLECTORS:
            if name in self.collected:
                continue
            if stalled and not self.guest.ok("true", timeout=15):
                self.fail("guest unreachable after a collection timeout; not collected: %s" % self.uncollected())
                self.r["salvage"] = "guest_unreachable"
                return False
            stalled = bool(getattr(self, name)())
            self.collected.add(name)
        if stalled and not self.guest.ok("true", timeout=15):  # the last collector timed out; nothing skipped
            self.fail("guest unreachable after a collection timeout; every collector ran")
            return False
        return True

    def collected_all(self):
        return set(self.COLLECTORS) <= self.collected

    def uncollected(self):
        return ", ".join(n for n in self.COLLECTORS if n not in self.collected)

    def salvage(self):
        """Best-effort collection when an error or an interrupt ended the agent phase, or collection itself,
        before every collector completed: the agent's files are still in the clone until teardown deletes
        it (Lookout GUEST-OUTPUT F1). The guest session is ended first; a session end that ran in the guest
        counts as the reachability check, otherwise the guest is pinged once; the original failure stays
        first in the record; collection failures are fail reasons of their own; a guest that no longer
        answers is one too. Not tried when normal collection already found the guest gone (agy fix round 2)."""
        if "t_ready" not in self.r["timings"] or self.collected_all() or self.guest is None:
            return
        if self.r.get("salvage") == "guest_unreachable":  # collect_outputs stopped at a failed ping
            return
        self.log("salvage: the run ended (%s) with outputs uncollected: %s; collecting before teardown"
                 % (self.r.get("end_reason"), self.uncollected()))
        reachable = False
        if self.session is not None:
            try:
                try:
                    stop = self.session.end()  # never raises by contract; the attach ssh is let go regardless
                finally:
                    self.session.release()
                reachable = isinstance(stop, dict) and stop.get("rc") == 0
            except Exception as e:
                self.log("salvage: ending the guest session failed: %s" % e)
            self.r["session_stops"] = list(self.session.stops)
        if not reachable and not self.guest.ok("true", timeout=15):
            self.r["salvage"] = "guest_unreachable"
            self.fail("salvage: guest unreachable; not collected: %s" % self.uncollected())
            return
        self.collect_outputs()
        if not self.collected_all():  # the ping between collectors failed (Lookout round 2 F1-R2): recorded there
            return
        self.r["salvage"] = "collected"
        self.mark("t_collect", salvage=True)

    def collect_home(self):
        h = self.r.get("home_changes")
        if h is None:
            return
        try:
            self._collect_home(h)
        except subprocess.TimeoutExpired as e:
            self.fail("home capture timed out: %s" % str(e)[:200])
            return True

    def _collect_home(self, h):
        g = self.guest
        l = g.run(project.HOME_LIST_CMD % ("~", project.home_prunes(self.cfg)), timeout=300)
        if l.returncode != 0:
            self.fail("home listing after the agent failed: exit %d %s" % (l.returncode, l.stderr.strip()[-200:]))
            return
        self.save("home-after.txt", l.stdout)
        after, bad = project.parse_listing(l.stdout)
        h["bad_names"] = self.home_bad_before + bad  # not a running sum: salvage may rerun this collector
        if h["bad_names"]:
            self.fail("home listing: %d names refused (control character or backslash)" % h["bad_names"])
        h.update(project.classify(self.home_before or {}, after))
        info, keep = project.cap_changes(after, h["added"] + h["modified"])
        h.update(info)
        if h["capped"]:
            self.fail("home changes archive capped at %d bytes: %d bytes in %d files, lists complete, archive partial"
                      % (project.HOME_CAP_BYTES, h["bytes"], h["files"]))
        if not keep:
            return
        t = g.run("cat > %s && tar czf %s -C ~ -T %s" % (project.HOME_LIST, project.HOME_CHANGED, project.HOME_LIST),
                  input=project.list_input(keep), timeout=300)
        if t.returncode != 0:
            self.fail("home changed-files archive failed: %s" % t.stderr.strip()[-200:])
        elif g.pull(project.HOME_CHANGED, self.rec_dir / "home-changed.tgz"):
            h["archive"] = "home-changed.tgz"
        else:
            self.fail("home changed-files archive could not be pulled")

    def experiment(self):
        prompt = self.cfg["prompt"].replace("{run_id}", self.id)
        self.session.deliver(prompt)
        self.mark("t_deliver")

        def on_dialog(d, approved, why, action):
            self.r["dialogs"].append({"at": now(), "kind": d.kind, "apps": d.apps, "grants": d.grants, "extra": d.extra,
                                      "warnings": d.warnings, "approved": approved, "action": action, "why": why, "text": d.text})
            self.log("dialog %s apps=%s grants=%s %s (%s)" % (d.kind, d.apps, d.grants, action, why))

        def on_reprompt(text):
            self.r["reprompts"].append({"at": now(), "text": text})
            self.log("reprompt sent after denial")
            self.save("pane-reprompt-%d.txt" % len(self.r["reprompts"]), self.session.pane())

        self.r["reprompts"] = []
        reprompt = self.cfg.get("refusal_reprompt")
        reprompt = reprompt.replace("{run_id}", self.id) if reprompt else None
        done = self.cfg["done"]["guest_file"]
        self.r["end_reason"] = self.session.watch(self.cfg, done, self.cfg["deadline_s"], on_dialog,
                                                  reprompt=reprompt, on_reprompt=on_reprompt)
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
        (round 5 R5-F3: no empty-archive fallback). A guest timeout is a fail reason like the other
        collectors' (agy fix round F1), so collection goes on to t_collect and the artefacts."""
        try:
            tar = self.guest.run("tar czf ~/results.tgz -C %s ." % self.cfg["results"])  # path checked by config.load
            if tar.returncode != 0:
                self.fail("results archive of %s failed: %s" % (self.cfg["results"], tar.stderr.strip()[-200:]))
            elif not self.guest.pull("results.tgz", self.res_dir / "results.tgz"):
                self.fail("results archive could not be pulled")
        except subprocess.TimeoutExpired as e:
            self.fail("results archive timed out: %s" % str(e)[:200])
            return True

    def collect_interactive(self):
        g = self.guest
        if not g.pull("~/.cuvm/pane.log", self.rec_dir / "pane.log"):
            self.log("pane.log could not be pulled")
        files = self.transcripts() or []
        a = {"transcript_files": [], "tools_used": [], "forbidden_tools_attempted": [], "forbidden_tools_used": [],
             "request_access_granted": [], "browser_navigated_hosts": [], "browser_console_errors": 0, "browser_network_failed": 0}
        for f in files:
            local = self.res_dir / "transcripts" / pathlib.Path(f).name
            if not g.pull(f, local) or not local.exists():
                self.fail("transcript %s could not be pulled" % f)
                continue
            a["transcript_files"].append(str(local.relative_to(self.rec_dir)))
            t = transcript.extract(local.read_text().splitlines(), *browser.tool_rules(self.cfg))
            for k in ("tools_used", "forbidden_tools_attempted", "forbidden_tools_used", "request_access_granted",
                      "browser_navigated_hosts"):
                a[k] = sorted(set(a[k]) | set(t[k]))
            for k in ("browser_console_errors", "browser_network_failed"):
                a[k] += t.get(k, 0)
        a["granted_outside_config"] = sorted(set(a["request_access_granted"]) - set(self.cfg["apps"]))
        a["pane_log"] = "pane.log" if (self.rec_dir / "pane.log").exists() else None
        self.collect_outputs()  # a guest that stopped answering is recorded there (agy fix round 2)
        if self.collected_all():
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
        alive = self.collect_outputs()  # False: the guest stopped answering, recorded there (agy fix round 2)
        if self.collected_all():
            self.mark("t_collect")
        a = {"transcript_file": str(chosen.relative_to(self.rec_dir)) if chosen else None}
        t = {}
        if chosen:
            t = transcript.extract(chosen.read_text().splitlines(), *browser.tool_rules(self.cfg))
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
            if "browser_navigated" in c:
                host, seen = c["browser_navigated"], t.get("browser_hosts_before_input", [])
                checks.append({"browser_navigated": host, "seen_before_input": seen, "pass": host in seen})
                if host not in seen:
                    self.fail("check browser_navigated %s: no successful navigate before the final input (saw %s)"
                              % (host, seen))
                continue
            path, want = c["guest_file_equals"]
            local = self.res_dir / "checks" / ("%d-%s" % (i, pathlib.Path(path).name))
            if alive:  # no 300 s scp per check against a guest that stopped answering
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
                if "dev_server" in self.cfg:
                    self.stop_dev_server()
                self.guest.run("tmux kill-server 2>/dev/null; true", timeout=20)
                rm = Session(self.guest, self.gw, getattr(self, "port", 0)).remove_login()
                self.save("remove-login.txt", rm.stdout + rm.stderr)
                t["login_removed"] = rm.returncode == 0
                self.r["auth"] = dict(self.r.get("auth", {}), removed_at=now())
                if not t["login_removed"]:
                    self.fail("login removal or post E2 failed")
                if getattr(self, "port", None) and "t_pre" in self.r["timings"]:
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
                    self.salvage()
                except Exception as e:  # the clone is still deleted below; the loss is recorded
                    self.r["salvage"] = "error"
                    self.fail("salvage error: %s" % e)
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
