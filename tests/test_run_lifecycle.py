"""Fault paths in teardown, leak checks, collection and the CLI loop, with every host and guest
command stubbed (no VM, ssh or tart)."""
import json
import os
import pathlib
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from cuvm import __main__ as cli, config, run, session
from cuvm.run import Run

GOLD = {"disk": "d", "config": "c"}


def done(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def bare(tmp):
    r = Run.__new__(Run)
    r.id, r.vm, r.out = "t-1", "cuvm-t-1", tmp
    r.rec_dir = tmp / "t-1"
    r.res_dir = r.rec_dir / "results"
    r.res_dir.mkdir(parents=True)
    inputs = tmp / "inputs"
    inputs.mkdir()
    (inputs / "known.txt").write_text("k")
    r.cfg = {"image": "gold", "inputs": str(inputs), "results": "~/exp/results",
             "done": {"guest_file": "~/Desktop/x.txt"}, "checks": [], "apps": ["TextEdit"]}
    r.image = {"image_sha256": GOLD}
    r.keep_on_fail, r.t0, r.ip, r.mode = False, time.monotonic(), None, "scripted"
    r.tart_proc = r.proxy_proc = r.guest = r.session = None
    r.collected, r.project_before, r.home_bad_before = set(), {}, 0
    r.r = {"run_id": r.id, "timings": {}, "phases": [], "dialogs": [], "fail_reasons": [],
           "inputs_sha256_before": {"known.txt": run.sha256(inputs / "known.txt")}}
    return r


def host(**rc):
    """Fake run.sh: tart list, pgrep and tart delete answer with the given return codes."""
    def sh(cmd, timeout=120):
        key = {"list": "list", "delete": "delete"}.get(cmd[1], cmd[0]) if cmd[0] == "tart" else cmd[0]
        return done(rc.get(key, 1 if key == "pgrep" else 0))
    return mock.patch.object(run, "sh", side_effect=sh)


class LeakCheckTest(unittest.TestCase):
    def setUp(self):
        self.r = bare(pathlib.Path(tempfile.mkdtemp()))
        p = mock.patch.object(run.imgmod, "hashes", return_value=GOLD)
        p.start()
        self.addCleanup(p.stop)

    def test_clean(self):
        with host():
            self.r.leaks(False)
        self.assertEqual(self.r.r["fail_reasons"], [])
        self.assertIs(self.r.r["leaks"]["vm_listed"], False)

    def test_tart_list_error_is_not_absence(self):
        with host(list=1):
            self.r.leaks(False)
        self.assertIsNone(self.r.r["leaks"]["vm_listed"])
        self.assertTrue(any("leak check" in f for f in self.r.r["fail_reasons"]))

    def test_pgrep_error_is_not_absence(self):
        with host(pgrep=2):
            self.r.leaks(False)
        self.assertIsNone(self.r.r["leaks"]["tart_run_proc"])
        self.assertTrue(self.r.r["fail_reasons"])

    def test_golden_hash_error_is_recorded_not_raised(self):
        with host(), mock.patch.object(run.imgmod, "hashes", side_effect=FileNotFoundError("disk.img")):
            self.r.leaks(False)
        self.assertIsNone(self.r.r["leaks"]["golden_unchanged"])
        self.assertTrue(any("disk.img" in f for f in self.r.r["fail_reasons"]))

    def test_delete_failure_fails_the_run(self):
        with host(delete=1):
            self.r.teardown()
        self.assertTrue(any("teardown delete" in f for f in self.r.r["fail_reasons"]))


class FinalizeTest(unittest.TestCase):
    def test_record_written_and_signals_restored_when_teardown_raises(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        r = bare(tmp)
        for phase in ("preflight_host", "clone_boot", "start_proxy", "connect", "start_session",
                      "experiment", "collect"):
            setattr(r, phase, lambda: None)
        r.run_probes = lambda when: True
        r.teardown = mock.Mock(side_effect=FileNotFoundError("disk.img"))
        before = signal.getsignal(signal.SIGTERM)
        self.assertFalse(r.execute())
        rec = json.loads((r.rec_dir / "record.json").read_text())
        self.assertEqual(rec["verdict"], "FAIL")
        self.assertTrue(any("disk.img" in f for f in rec["fail_reasons"]))
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)
        self.assertEqual(len((tmp / "runs.jsonl").read_text().splitlines()), 1)


class FakeGuest:
    def __init__(self, tar_rc=0, pull_ok=True, tar_timeout=False):
        self.cmds, self.tar_rc, self.pull_ok, self.tar_timeout = [], tar_rc, pull_ok, tar_timeout

    def run(self, cmd, timeout=120, input=None):
        self.cmds.append(cmd)
        if self.tar_timeout and "tar " in cmd:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return done(self.tar_rc if "tar " in cmd else 0)

    def ok(self, cmd, timeout=30):
        return False

    def pull(self, remote, local):
        return self.pull_ok if remote == "results.tgz" else False


class CollectTest(unittest.TestCase):
    def collect(self, results="~/exp/results", **kw):
        r = bare(pathlib.Path(tempfile.mkdtemp()))
        r.cfg["results"] = results
        r.guest, r.session = FakeGuest(**kw), mock.Mock(pane=lambda history=False: "")
        r.collect()
        return r

    def test_archives_the_configured_directory(self):
        r = self.collect("~/exp/other")
        self.assertIn("tar czf ~/results.tgz -C ~/exp/other .", r.guest.cmds)
        self.assertFalse(any("results archive" in f for f in r.r["fail_reasons"]))

    def test_archive_failure_fails_the_run(self):
        r = self.collect(tar_rc=2)
        self.assertTrue(any("results archive" in f for f in r.r["fail_reasons"]))

    def test_pull_failure_fails_the_run(self):
        r = self.collect(pull_ok=False)
        self.assertTrue(any("results archive" in f for f in r.r["fail_reasons"]))

    def test_archive_timeout_is_a_fail_reason_and_collection_finishes(self):
        """agy fix-round F1: a tar or scp timeout in archive_results was the one uncaught TimeoutExpired
        among the three collectors; it cut collect() short before t_collect and the artefacts."""
        r = self.collect(tar_timeout=True)
        self.assertTrue(any("results archive" in f and "timed out" in f for f in r.r["fail_reasons"]))
        self.assertIn("t_collect", r.r["timings"])
        self.assertIn("artefacts", r.r)
        self.assertEqual(r.collected, {"collect_project", "collect_home", "archive_results"})


class ResultsConfigTest(unittest.TestCase):
    def load(self, results):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "inputs").mkdir()
        (d / "inputs/known.txt").write_text("k")
        cfg = json.loads((pathlib.Path(__file__).parent.parent / "experiments/smoke/config.json").read_text())
        cfg["results"] = results
        (d / "config.json").write_text(json.dumps(cfg))
        return config.load(d / "config.json")[0]

    def test_home_relative_paths_accepted(self):
        self.assertEqual(self.load("~/exp/other-results")["results"], "~/exp/other-results")

    def test_unsafe_paths_rejected(self):
        for bad in ("/var/x", "~/../x", "~/a b", "~/x;rm", "~", "~/"):
            with self.assertRaises(config.ConfigError, msg=bad):
                self.load(bad)


class GuestPasswordTest(unittest.TestCase):
    def test_host_override_reaches_the_session_start(self):
        g = mock.Mock()
        with mock.patch.dict(os.environ, {"CUVM_GUEST_PW": "s3cret"}):
            session.Session(g, "192.168.128.1", 8888).start(["--tools", "ToolSearch"])
        cmd = g.run.call_args[0][0]
        self.assertIn("-p s3cret ", cmd)
        self.assertNotIn("CUVM_GUEST_PW", cmd)


class CliTest(unittest.TestCase):
    def test_repeat_must_be_positive(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            cli.main(["run", "nonexistent.json", "--repeat", "0"])

    def test_interrupt_stops_the_series(self):
        made = []

        class FakeRun:
            def __init__(self, *a, **k):
                made.append(self)
                self.r = {"end_reason": "operator_interrupt"}

            def execute(self):
                return False
        with mock.patch.object(run, "Run", FakeRun), mock.patch("builtins.print"):
            self.assertEqual(cli.main(["run", "c.json", "--repeat", "3"]), 1)
        self.assertEqual(len(made), 1)


class TokenIsolationTest(unittest.TestCase):
    def setUp(self):
        self.r = bare(pathlib.Path(tempfile.mkdtemp()))
        self.r.dummy = "sk-ant-oat01-cuvm-dummy-" + "ab" * 24
        self.r.cfg.update(plan="max", agent_args=[])
        self.r.gw, self.r.port = "192.0.2.1", 1

    def test_e3_mismatch_stops_before_the_agent_starts_and_login_is_marked_for_removal(self):
        s = mock.Mock()
        s.inject.return_value = done(0, "written")
        s.e3.return_value = done(0, "0" * 64 + "\n")
        with mock.patch.object(run, "Session", return_value=s):
            with self.assertRaisesRegex(RuntimeError, "E3"):
                self.r.start_session()
        self.assertEqual(s.inject.call_args.args, ("max", self.r.dummy))
        s.start.assert_not_called()
        self.assertIn("attempted_at", self.r.r["auth"])
        self.assertEqual(self.r.r["auth"]["method"], "dummy-login-host-swap")
        self.assertFalse(self.r.r["probes"]["session"]["E3"]["pass"])
        self.assertNotIn(self.r.dummy, json.dumps(self.r.r))

    def test_e3_match_passes(self):
        import hashlib
        s = mock.Mock()
        s.inject.return_value = done(0, "")
        s.e3.return_value = done(0, hashlib.sha256(self.r.dummy.encode()).hexdigest() + "\n")
        s.start.return_value = done(1)
        with mock.patch.object(run, "Session", return_value=s):
            with self.assertRaisesRegex(RuntimeError, "session start"):
                self.r.start_session()
        self.assertTrue(self.r.r["probes"]["session"]["E3"]["pass"])

    def test_inject_sends_token_on_stdin_only(self):
        g = mock.Mock(ip="192.0.2.4", user="admin", known_hosts="/kh")
        with mock.patch.object(session.subprocess, "run", return_value=done()) as sp:
            session.Session(g, "192.0.2.1", 1).inject("max", self.r.dummy)
        argv, kw = sp.call_args.args[0], sp.call_args.kwargs
        self.assertEqual(kw["input"], self.r.dummy)
        self.assertNotIn(self.r.dummy, " ".join(argv) + json.dumps(kw["env"]))

    def test_dummy_is_per_run(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        with mock.patch.object(run.cfgmod, "load", return_value=({"image": "gold"}, "h")), \
                mock.patch.object(run.imgmod, "load", return_value=(tmp / "gold.json", {})):
            a, b = (Run(tmp / "c" / "config.json", out=tmp / "runs").dummy for _ in range(2))
        self.assertRegex(a, r"^sk-ant-oat01-cuvm-dummy-[0-9a-f]{48}$")
        self.assertNotEqual(a, b)

    def test_proxy_refuses_to_start_without_host_token(self):
        self.r.gw, self.r.ip = "127.0.0.1", "127.0.0.1"
        self.r.cfg["net_allow"] = str(run.PKG / "data" / "allow-run.txt")
        self.r.r["net"] = {}
        with mock.patch.dict(os.environ, CUVM_KEYCHAIN_ITEM="cuvm-test-absent-item-%d" % time.time_ns()):
            with self.assertRaisesRegex(RuntimeError, "egress proxy did not start"):
                self.r.start_proxy()
        self.r.proxy_proc.wait(10)
        self.r.proxy_proc.stdout.close()
        self.assertNotEqual(self.r.proxy_proc.returncode, 0)
        self.assertIn("keychain", (self.r.rec_dir / "egress.stderr").read_text())

    def check_log(self, *events):
        self.r.proxy_log = self.r.rec_dir / "egress.jsonl"
        self.r.proxy_log.write_text("".join(json.dumps(e) + "\n" for e in events))
        self.r.r["net"] = {"allow": ["api.anthropic.com", "claude.ai"]}
        self.r.check_proxy_log()
        return self.r.r["fail_reasons"]

    def test_proxy_log_clean(self):
        self.assertEqual(self.check_log({"ev": "request", "host": "api.anthropic.com", "outcome": "ok", "swapped": ["authorization"]},
                                        {"ev": "tunnel", "host": "claude.ai"}), [])
        self.assertEqual(self.r.r["proxy"]["swapped"], 1)

    def test_proxy_log_real_token_in_response_fails(self):
        self.assertTrue(any("real token" in f for f in self.check_log({"ev": "real_in_response", "host": "api.anthropic.com"})))

    def test_proxy_log_outside_host_and_unauthorized_fail(self):
        f = self.check_log({"ev": "tunnel", "host": "evil.example"}, {"ev": "unauthorized", "client": "192.0.2.1"})
        self.assertTrue(any("outside net_allow" in x for x in f) and any("unauthorized" in x for x in f))


if __name__ == "__main__":
    unittest.main()


LSOF_HEAD = "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
LSOF_LO = LSOF_HEAD + "node 12 admin 23u IPv4 0x1 0t0 TCP 127.0.0.1:5173 (LISTEN)\n"


class DevGuest:
    """Answers guest commands by substring; a list answer is consumed one call at a time."""
    def __init__(self, answers=None):
        self.answers, self.cmds, self.pulled, self.inputs = dict(answers or {}), [], {}, []

    def run(self, cmd, timeout=120, input=None):
        self.cmds.append(cmd)
        self.inputs.append(input)
        for k, v in self.answers.items():
            if k in cmd:
                if isinstance(v, list):
                    v = v.pop(0) if v else ""
                return done(*v) if isinstance(v, tuple) else done(0, v)
        return done(0, "")

    def ok(self, cmd, timeout=30):
        return self.run(cmd).returncode == 0

    def wait(self, check, timeout, every=1.0):
        return check()

    def pull(self, remote, local):
        local = pathlib.Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(self.answers.get("pull:" + remote, ""))
        self.pulled[remote] = str(local)
        return True


def webdev(tmp, **answers):
    r = bare(tmp)
    r.cfg.update(project="project.tgz", project_top="app", browser="webkit",
                 dev_server={"command": "npm run dev -- --host 127.0.0.1 --port 5173 --strictPort", "port": 5173, "ready_s": 5})
    r.gw, r.port = "192.0.2.1", 4321
    r.guest = DevGuest(answers)
    r.proxy_log = r.rec_dir / "egress.jsonl"
    r.proxy_log.write_text("")
    return r


class DevServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def test_unpack_records_manifest_and_timing(self):
        r = webdev(self.tmp, **{"shasum -a 256": "a" * 64 + "  ./app/index.html\n"})
        r.unpack_project()
        self.assertTrue(any("tar xzf '/Volumes/My Shared Files/inputs/project.tgz' -C ~/exp/project" in c for c in r.guest.cmds))
        self.assertEqual(r.r["project"]["top_dir"], "app")
        self.assertEqual(r.r["project"]["dir"], "~/exp/project/app")
        man = [c for c in r.guest.cmds if "shasum -a 256" in c]
        self.assertEqual(len(man), 1)
        self.assertIn("cd ~/exp/project &&", man[0])  # the root, not <top> (the guest-output design)
        self.assertNotIn("~/exp/project/app", man[0])
        self.assertEqual(r.r["project"]["manifest"], {"app/index.html": "a" * 64})
        self.assertIn("t_unpack", r.r["timings"])

    def test_unpack_failure_raises(self):
        r = webdev(self.tmp, **{"tar xzf": (1, "")})
        with self.assertRaisesRegex(RuntimeError, "unpack"):
            r.unpack_project()

    def test_manifest_failure_raises(self):
        r = webdev(self.tmp, **{"shasum -a 256": (1, "")})
        with self.assertRaisesRegex(RuntimeError, "manifest"):
            r.unpack_project()

    def test_pane_capture_falls_back_to_the_log(self):
        r = webdev(self.tmp, curl="000", lsof=LSOF_LO)
        with self.assertRaises(RuntimeError):
            r.start_dev_server()
        cap = [c for c in r.guest.cmds if "capture-pane" in c]
        self.assertEqual(len(cap), 1)
        self.assertIn("|| cat ~/exp/results/dev-server.log", cap[0])

    def test_starts_on_its_own_socket_and_records_d1(self):
        r = webdev(self.tmp, curl="200", lsof=LSOF_LO)
        r.start_dev_server()
        start = [c for c in r.guest.cmds if "tmux -L dev new -d -s dev -c ~/exp/project/app " in c]
        self.assertEqual(len(start), 1)
        self.assertIn("~/exp/results/dev-server.log", start[0])
        self.assertIn("HTTPS_PROXY=http://192.0.2.1:4321", start[0])
        self.assertFalse(any("tmux new" in c and "-L dev" not in c for c in r.guest.cmds))
        d1 = r.r["probes"]["pre"]["D1"]
        self.assertTrue(d1["pass"], d1)
        self.assertEqual(r.r["dev_server"]["port"], 5173)
        self.assertEqual(r.r["dev_server"]["listen"], ["127.0.0.1:5173"])
        self.assertIn("t_dev_server", r.r["timings"])

    def test_wildcard_listener_fails_closed(self):
        r = webdev(self.tmp, curl="200", lsof=LSOF_HEAD + "node 1 admin 2u IPv4 0 0t0 TCP *:5173 (LISTEN)\n")
        with self.assertRaisesRegex(RuntimeError, "D1"):
            r.start_dev_server()
        self.assertFalse(r.r["probes"]["pre"]["D1"]["pass"])
        self.assertTrue(any("tmux -L dev capture-pane" in c for c in r.guest.cmds))
        self.assertTrue((r.rec_dir / "dev-server-pane.txt").exists())

    def test_not_ready_fails_closed(self):
        r = webdev(self.tmp, curl="000", lsof=LSOF_LO)
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            r.start_dev_server()
        self.assertTrue((r.rec_dir / "dev-server-pane.txt").exists())


class ProbeMergeTest(unittest.TestCase):
    """B1 is out of scope here (no image record): the browser key is dropped so run_probes skips it."""
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def probe_run(self, r):
        del r.cfg["browser"]
        r.r["host"] = {"en0_addr": "192.0.2.9", "listeners_wildcard": []}
        s = mock.Mock()
        s.e2.return_value = done(0, "ok")
        with mock.patch.object(run.probes, "commands", return_value=[]), mock.patch.object(run.probes, "judge", return_value={}), \
                mock.patch.object(run, "Session", return_value=s), mock.patch.object(run, "sha256", return_value="k"):
            return r.run_probes("pre")

    def test_prerecorded_entry_counts_in_the_verdict(self):
        r = webdev(self.tmp)
        r.r["probes"] = {"pre": {"D1": {"exit": 0, "out": "", "pass": False, "why": "wildcard"}}}
        self.assertFalse(self.probe_run(r))
        self.assertIn("pre probes failed: ['D1']", r.r["fail_reasons"])
        self.assertIn("E2", r.r["probes"]["pre"])
        self.assertEqual(r.egress_offset, 0)

    def test_offset_taken_after_pre_probes(self):
        """The probe batch itself logs a loopback refusal (N2b-lo); the D2 window starts after it."""
        r = webdev(self.tmp)
        log = r.proxy_log
        log.write_text('{"ev": "refused", "host": "example.com"}\n')
        real_run = r.guest.run

        def run(cmd, timeout=120, input=None):
            if input and "p(){" in input:  # the probe script: the batch refusal lands while it runs
                with open(log, "a") as f:
                    f.write('{"ev": "refused", "host": "127.0.0.1", "port": "443", "why": "not allowlisted"}\n')
            return real_run(cmd, timeout, input)
        r.guest.run = run
        self.assertTrue(self.probe_run(r))
        self.assertEqual(r.egress_offset, len(log.read_bytes()))
        self.assertIn("127.0.0.1", log.read_text())


class TeardownDevTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def teardown(self, r):
        s = mock.Mock()
        s.remove_login.return_value = done(0, "")
        r.run_probes = mock.Mock(return_value=True)
        r.ip = "192.0.2.5"
        with host(), mock.patch.object(run, "Session", return_value=s), mock.patch.object(run.imgmod, "hashes", return_value=GOLD):
            r.teardown()
        return r

    def test_post_probes_need_t_pre(self):
        r = webdev(self.tmp)
        r.r["probes"] = {"pre": {"D1": {"pass": True}}}
        self.teardown(r)
        r.run_probes.assert_not_called()
        r = webdev(pathlib.Path(tempfile.mkdtemp()))
        r.r["probes"] = {"pre": {"D1": {"pass": True}}}
        r.r["timings"]["t_pre"] = 1.0
        self.teardown(r)
        r.run_probes.assert_called_once_with("post")

    def test_dev_socket_killed_first_and_d2_judged_over_the_session_window(self):
        r = webdev(self.tmp, **{"capture-pane": "VITE ready\n"})
        pre = '{"ev": "refused", "host": "127.0.0.1", "port": "443", "why": "not allowlisted"}\n'
        r.proxy_log.write_text(pre)
        r.egress_offset = len(pre.encode())
        r.proxy_log.write_text(pre + '{"ev": "refused", "host": "localhost", "why": "not CONNECT"}\n')
        r.r["timings"]["t_pre"] = 1.0
        self.teardown(r)
        cmds = r.guest.cmds
        kill_dev = next(i for i, c in enumerate(cmds) if "tmux -L dev kill-server" in c)
        kill_all = next(i for i, c in enumerate(cmds) if c.startswith("tmux kill-server"))
        self.assertLess(kill_dev, kill_all)
        self.assertEqual((r.rec_dir / "dev-server-pane.txt").read_text(), "VITE ready\n")
        d2 = r.r["probes"]["post"]["D2"]
        self.assertFalse(d2["pass"])
        self.assertEqual(d2["refused"], 1)

    def test_no_d2_without_offset(self):
        r = webdev(self.tmp)
        r.r["timings"]["t_pre"] = 1.0
        self.teardown(r)
        self.assertNotIn("D2", r.r.get("probes", {}).get("post", {}))


class CollectProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def run_collect(self, after, diff=(1, "")):
        r = webdev(self.tmp, **{"shasum -a 256": after, "diff -ruN": diff, "pull:~/.cuvm/project.patch": "--- a\n+++ b\n"})
        r.r["project"] = {"archive": "project.tgz", "top_dir": "app", "manifest": {"app/index.html": "a" * 64, "app/src/main.js": "b" * 64}}
        r.collect_project()
        return r

    def test_changed_files_are_classified_and_archived(self):
        r = self.run_collect("a" * 64 + "  ./app/index.html\n" + "c" * 64 + "  ./app/src/main.js\n" + "d" * 64 + "  ./app/new.css\n")
        p = r.r["project"]
        self.assertEqual((p["added"], p["modified"], p["deleted"]), (["app/new.css"], ["app/src/main.js"], []))
        self.assertEqual(p["patch"], "project.patch")
        self.assertEqual(p["changed_archive"], "project-changed.tgz")
        tar = [c for c in r.guest.cmds if "tar czf ~/.cuvm/project-changed.tgz" in c]
        self.assertEqual(len(tar), 1)
        self.assertIn("-C ~/exp/project -T", tar[0])
        self.assertEqual(r.guest.inputs[r.guest.cmds.index(tar[0])], "./app/new.css\n./app/src/main.js\n")
        self.assertTrue(any("diff -ruN --exclude node_modules ~/.cuvm/project-clean ~/exp/project >" in c for c in r.guest.cmds))
        self.assertEqual((r.rec_dir / "project.patch").read_text(), "--- a\n+++ b\n")

    def test_a_rerun_after_an_interrupt_keeps_the_before_manifest(self):
        """agy fix-round F1: salvage reruns a collector an interrupt cut short; the before manifest was
        popped from the record on the first attempt and must not read as empty (everything added)."""
        r = self.run_collect("a" * 64 + "  ./app/index.html\n" + "c" * 64 + "  ./app/src/main.js\n")
        self.assertEqual((r.r["project"]["added"], r.r["project"]["modified"]), ([], ["app/src/main.js"]))
        r.collect_project()
        self.assertEqual((r.r["project"]["added"], r.r["project"]["modified"]), ([], ["app/src/main.js"]))
        self.assertEqual(json.loads((r.rec_dir / "project-manifest.json").read_text())["before"]["app/index.html"], "a" * 64)

    def test_a_directory_beside_the_project_is_collected(self):
        """Bead cuvm-vzi: run web-dev-20261107T204056Z-fce0 built ~/exp/project/screensavers beside fixture/ and
        the record showed nothing; the snapshot now covers the ~/exp/project root."""
        r = self.run_collect("a" * 64 + "  ./app/index.html\n" + "b" * 64 + "  ./app/src/main.js\n"
                             + "e" * 64 + "  ./screensavers/src/main.js\n" + "f" * 64 + "  ./screensavers/index.html\n")
        p = r.r["project"]
        self.assertEqual((p["added"], p["modified"], p["deleted"]), (["screensavers/index.html", "screensavers/src/main.js"], [], []))
        man = [c for c in r.guest.cmds if "shasum -a 256" in c]
        self.assertEqual(len(man), 1)
        self.assertIn("cd ~/exp/project &&", man[0])
        self.assertNotIn("~/exp/project/app", man[0])
        tar = [c for c in r.guest.cmds if "tar czf ~/.cuvm/project-changed.tgz" in c]
        self.assertEqual(r.guest.inputs[r.guest.cmds.index(tar[0])], "./screensavers/index.html\n./screensavers/src/main.js\n")
        self.assertEqual(p["changed_archive"], "project-changed.tgz")

    def test_no_change_means_no_archive(self):
        r = self.run_collect("a" * 64 + "  ./app/index.html\n" + "b" * 64 + "  ./app/src/main.js\n", diff=(0, ""))
        p = r.r["project"]
        self.assertEqual((p["added"], p["modified"], p["deleted"]), ([], [], []))
        self.assertIsNone(p["changed_archive"])
        self.assertFalse(any("project-changed.tgz" in c for c in r.guest.cmds))

    def test_manifest_failure_is_a_fail_reason_and_skips_the_archive(self):
        r = self.run_collect((1, "ssh: lost"))
        self.assertTrue(any("project manifest" in f for f in r.r["fail_reasons"]))
        self.assertIsNone(r.r["project"]["added"])
        self.assertFalse(any("project-changed.tgz" in c for c in r.guest.cmds))
        self.assertEqual(r.r["project"]["patch"], "project.patch")

    def test_changed_list_and_archive_are_one_guest_command(self):
        r = self.run_collect("c" * 64 + "  ./app/index.html\n" + "b" * 64 + "  ./app/src/main.js\n")
        tar = [c for c in r.guest.cmds if "project-changed.tgz" in c]
        self.assertEqual(len(tar), 1)
        self.assertTrue(tar[0].startswith("cat > ~/.cuvm/project-changed.txt && tar czf"))

    def test_unparsed_manifest_is_a_fail_reason(self):
        r = self.run_collect("\\" + "a" * 64 + "  ./app/logo\\\\v2.svg\n")
        self.assertTrue(any("unparsed manifest" in f for f in r.r["fail_reasons"]))
        self.assertIsNone(r.r["project"]["added"])

    def test_clean_unpack_excludes_node_modules(self):
        r = self.run_collect("a" * 64 + "  ./app/index.html\n" + "b" * 64 + "  ./app/src/main.js\n", diff=(0, ""))
        clean = [c for c in r.guest.cmds if "project-clean" in c and "tar xzf" in c]
        self.assertEqual(len(clean), 1)
        self.assertIn("--exclude '*/node_modules'", clean[0])

    def test_diff_error_is_a_fail_reason(self):
        r = self.run_collect("a" * 64 + "  ./app/index.html\n" + "b" * 64 + "  ./app/src/main.js\n", diff=(2, "diff: trouble"))
        self.assertTrue(any("project diff" in f for f in r.r["fail_reasons"]))


class InteractiveCountsTest(unittest.TestCase):
    def test_integer_fields_are_summed(self):
        r = bare(pathlib.Path(tempfile.mkdtemp()))
        r.mode = "interactive"
        g = DevGuest()
        g.pull = lambda remote, local: (pathlib.Path(local).parent.mkdir(parents=True, exist_ok=True) or
                                        pathlib.Path(local).write_text("") or True)
        r.guest = g
        r.transcripts = lambda: ["/x/a.jsonl", "/x/b.jsonl"]
        t = {"tools_used": [], "forbidden_tools_attempted": [], "forbidden_tools_used": [], "request_access_granted": [],
             "browser_navigated_hosts": [], "browser_console_errors": 2, "browser_network_failed": 1}
        with mock.patch.object(run.transcript, "extract", return_value=t):
            r.collect_interactive()
        self.assertEqual(r.r["artefacts"]["browser_console_errors"], 4)
        self.assertEqual(r.r["artefacts"]["browser_network_failed"], 2)


class CollectWithProjectTest(unittest.TestCase):
    """Scripted collect() with a project: project collection runs before the results archive, and a guest
    timeout inside it is a fail reason that does not stop the archive (Lookout F1)."""
    def collect(self, timeout_on=None):
        r = webdev(pathlib.Path(tempfile.mkdtemp()), **{"shasum -a 256": "a" * 64 + "  ./app/index.html\n"})
        r.r["project"] = {"archive": "project.tgz", "top_dir": "app", "manifest": {"app/index.html": "a" * 64}}
        r.session = mock.Mock(pane=lambda history=False: "")
        r.transcripts = lambda: []
        if timeout_on:
            real = r.guest.run

            def run(cmd, timeout=120, input=None):
                if timeout_on in cmd:
                    r.guest.cmds.append(cmd)
                    raise subprocess.TimeoutExpired(cmd, timeout)
                return real(cmd, timeout, input)
            r.guest.run = run
        r.collect()
        return r

    def test_project_collection_precedes_the_archive(self):
        r = self.collect()
        cmds = r.guest.cmds
        self.assertLess(next(i for i, c in enumerate(cmds) if "diff -ruN" in c),
                        next(i for i, c in enumerate(cmds) if c.startswith("tar czf ~/results.tgz")))
        self.assertEqual(r.r["project"]["patch"], "project.patch")

    def test_timeout_in_project_collection_still_archives_results(self):
        r = self.collect(timeout_on="diff -ruN")
        self.assertTrue(any("project collection timed out" in f for f in r.r["fail_reasons"]))
        self.assertTrue(any(c.startswith("tar czf ~/results.tgz") for c in r.guest.cmds))
        self.assertIn("t_collect", r.r["timings"])
        self.assertIn("artefacts", r.r)

    def test_timeout_on_the_first_manifest_keeps_the_before_inventory_and_null_fields(self):
        """Lookout R2-F3: a timeout before any change list exists still leaves the before-manifest on disk and
        every project field present and null, as docs/records.md promises."""
        r = self.collect(timeout_on="shasum -a 256")
        p = r.r["project"]
        keys = ("added", "modified", "deleted", "patch", "changed_archive")
        self.assertEqual({k: p.get(k, "missing") for k in keys}, dict.fromkeys(keys))
        self.assertNotIn("manifest", p)
        self.assertEqual(json.loads((r.rec_dir / "project-manifest.json").read_text()),
                         {"before": {"app/index.html": "a" * 64}, "after": None})
        self.assertTrue(any("project collection timed out" in f for f in r.r["fail_reasons"]))
        self.assertTrue(any(c.startswith("tar czf ~/results.tgz") for c in r.guest.cmds))
        self.assertIn("artefacts", r.r)


BEFORE = "1 1 5 ./Desktop/old.txt\n\0" + "1 1 1 ./gone.txt\n\0" + "1 1 2 ./reach.txt\n\0"
AFTER = "2 2 5 ./Desktop/old.txt\n\0" + "9 9 3 ./Desktop/new.txt\n\0" + "1 1 2 ./reach.txt\n\0"


def ready_session():
    s = mock.Mock()
    s.inject.return_value = done(0, "")
    s.start.return_value = done(0)
    s.wait_ready.return_value = (True, "")
    s.mcp_check.return_value = ({"computer-use": True}, "")
    s.reach_check.return_value = (True, "PONG")
    return s


class HomeBeforeListingTest(unittest.TestCase):
    """the guest-output design: the home listing before the agent runs, at the end of start_session."""
    def start(self, listing=BEFORE, timeout=False):
        import hashlib
        r = bare(pathlib.Path(tempfile.mkdtemp()))
        r.dummy = "sk-ant-oat01-cuvm-dummy-" + "ab" * 24
        r.cfg.update(plan="max", agent_args=[], results="~/exp/other")
        r.gw, r.port = "192.0.2.1", 1
        r.r["guest"] = {}
        r.guest = DevGuest({"lstat(": listing})
        if timeout:
            def run_(cmd, timeout=120, input=None):
                r.guest.cmds.append(cmd)
                raise subprocess.TimeoutExpired(cmd, timeout)
            r.guest.run = run_
        s = ready_session()
        s.e3.return_value = done(0, hashlib.sha256(r.dummy.encode()).hexdigest() + "\n")
        with mock.patch.object(run, "Session", return_value=s):
            r.start_session()
        return r

    def test_listing_taken_after_the_reach_check_and_before_ready(self):
        r = self.start()
        cmds = [c for c in r.guest.cmds if "lstat(" in c]
        self.assertEqual(len(cmds), 1)
        self.assertTrue(cmds[0].startswith("set -o pipefail; cd ~ && find . "), cmds[0])
        self.assertIn("-path ./exp/other ", cmds[0])
        self.assertNotIn("./exp/project", cmds[0])  # no project in this config
        self.assertEqual(r.r["home_changes"], {"added": None, "modified": None, "deleted": None, "archive": None,
                                               "files": None, "bytes": None, "capped": None, "bad_names": 0})
        self.assertEqual(r.home_before, {"Desktop/old.txt": (1, 1, 5), "gone.txt": (1, 1, 1), "reach.txt": (1, 1, 2)})
        self.assertEqual((r.rec_dir / "home-before.txt").read_text(), BEFORE)
        self.assertIn("t_ready", r.r["timings"])
        self.assertEqual(r.r["fail_reasons"], [])

    def test_listing_failure_disables_the_capture_and_the_run_goes_on(self):
        r = self.start(listing=(1, "", "find: ./x: Permission denied"))
        self.assertIsNone(r.r["home_changes"])
        self.assertTrue(any("home listing before the agent failed" in f and "Permission denied" in f for f in r.r["fail_reasons"]))
        self.assertIn("t_ready", r.r["timings"])

    def test_listing_timeout_disables_the_capture_and_the_run_goes_on(self):
        r = self.start(timeout=True)
        self.assertIsNone(r.r["home_changes"])
        self.assertTrue(any("home listing before the agent failed" in f and "timed out" in f for f in r.r["fail_reasons"]))
        self.assertIn("t_ready", r.r["timings"])

    def test_bad_names_are_counted_and_the_rest_kept(self):
        r = self.start(listing=BEFORE + "1 1 1 ./bad\nname\n\0")
        self.assertEqual(r.r["home_changes"]["bad_names"], 1)
        self.assertEqual(sorted(r.home_before), ["Desktop/old.txt", "gone.txt", "reach.txt"])
        self.assertEqual(r.r["fail_reasons"], [])


class HomeCollectTest(unittest.TestCase):
    """the guest-output design: collect_home in both modes, between the project collection and the results
    archive; every failure a fail reason."""
    def collect(self, after=AFTER, mode="scripted", before=None, timeout_on=None, pull_ok=True, disabled=False):
        r = webdev(pathlib.Path(tempfile.mkdtemp()),
                   **{"shasum -a 256": "a" * 64 + "  ./app/index.html\n", "lstat(": after, "pull:~/.cuvm/home-changed.tgz": "tgz"})
        r.r["project"] = {"archive": "project.tgz", "top_dir": "app", "manifest": {"app/index.html": "a" * 64}}
        r.r["home_changes"] = None if disabled else {"added": None, "modified": None, "deleted": None, "archive": None,
                                                     "files": None, "bytes": None, "capped": None, "bad_names": 0}
        r.home_before = project_listing(BEFORE) if before is None else before
        r.mode = mode
        r.session = mock.Mock(pane=lambda history=False: "")
        r.transcripts = lambda: []
        if not pull_ok:
            real = r.guest.pull
            r.guest.pull = lambda remote, local: False if "home-changed" in remote else real(remote, local)
        if timeout_on:
            real_run = r.guest.run

            def run_(cmd, timeout=120, input=None):
                if timeout_on in cmd:
                    r.guest.cmds.append(cmd)
                    raise subprocess.TimeoutExpired(cmd, timeout)
                return real_run(cmd, timeout, input)
            r.guest.run = run_
        r.collect()
        return r

    def idx(self, r, needle):
        return next(i for i, c in enumerate(r.guest.cmds) if needle in c)

    def test_changes_are_classified_and_archived_between_project_and_results(self):
        r = self.collect()
        h = r.r["home_changes"]
        self.assertEqual((h["added"], h["modified"], h["deleted"]), (["Desktop/new.txt"], ["Desktop/old.txt"], ["gone.txt"]))
        self.assertEqual((h["files"], h["bytes"], h["capped"], h["archive"], h["bad_names"]), (2, 8, False, "home-changed.tgz", 0))
        tar = [c for c in r.guest.cmds if "home-changed.tgz" in c]
        self.assertEqual(tar, ["cat > ~/.cuvm/home-changed.txt && tar czf ~/.cuvm/home-changed.tgz -C ~ -T ~/.cuvm/home-changed.txt"])
        self.assertEqual(r.guest.inputs[r.guest.cmds.index(tar[0])], "./Desktop/new.txt\n./Desktop/old.txt\n")
        self.assertLess(self.idx(r, "diff -ruN"), self.idx(r, "lstat("))
        self.assertLess(self.idx(r, "lstat("), self.idx(r, "tar czf ~/results.tgz"))
        self.assertEqual((r.rec_dir / "home-after.txt").read_text(), AFTER)
        self.assertTrue((r.rec_dir / "home-changed.tgz").exists())
        self.assertEqual([f for f in r.r["fail_reasons"] if "home" in f], [])  # the mocked run has no transcript
        self.assertIn("t_collect", r.r["timings"])

    def test_interactive_mode_collects_the_home_too(self):
        with mock.patch.object(run.transcript, "extract"):
            r = self.collect(mode="interactive")
        h = r.r["home_changes"]
        self.assertEqual(h["archive"], "home-changed.tgz")
        self.assertLess(self.idx(r, "diff -ruN"), self.idx(r, "lstat("))
        self.assertLess(self.idx(r, "lstat("), self.idx(r, "tar czf ~/results.tgz"))
        self.assertIn("artefacts", r.r)

    def test_no_change_means_no_archive(self):
        r = self.collect(after=BEFORE)
        h = r.r["home_changes"]
        self.assertEqual((h["added"], h["modified"], h["deleted"]), ([], [], []))
        self.assertEqual((h["files"], h["bytes"], h["capped"], h["archive"]), (0, 0, False, None))
        self.assertFalse(any("home-changed.tgz" in c for c in r.guest.cmds))

    def test_timeout_is_a_fail_reason_and_the_results_archive_follows(self):
        r = self.collect(timeout_on="lstat(")
        self.assertTrue(any("home capture timed out" in f for f in r.r["fail_reasons"]))
        self.assertTrue(any(c.startswith("tar czf ~/results.tgz") for c in r.guest.cmds))
        h = r.r["home_changes"]
        self.assertEqual({k: h[k] for k in ("added", "modified", "deleted", "archive", "files", "bytes", "capped")},
                         dict.fromkeys(("added", "modified", "deleted", "archive", "files", "bytes", "capped")))
        self.assertIn("t_collect", r.r["timings"])

    def test_listing_failure_is_a_fail_reason_with_null_fields(self):
        r = self.collect(after=(1, "", "find: boom"))
        self.assertTrue(any("home listing after the agent failed" in f and "boom" in f for f in r.r["fail_reasons"]))
        self.assertIsNone(r.r["home_changes"]["added"])
        self.assertFalse(any("home-changed.tgz" in c for c in r.guest.cmds))
        self.assertTrue(any(c.startswith("tar czf ~/results.tgz") for c in r.guest.cmds))

    def test_cap_truncates_the_archive_not_the_lists(self):
        with mock.patch.object(run.project, "HOME_CAP_BYTES", 4):
            r = self.collect()
        h = r.r["home_changes"]
        self.assertEqual((h["added"], h["modified"]), (["Desktop/new.txt"], ["Desktop/old.txt"]))
        self.assertEqual((h["files"], h["bytes"], h["capped"], h["archive"]), (2, 8, True, "home-changed.tgz"))
        tar = [c for c in r.guest.cmds if "home-changed.tgz" in c]
        self.assertEqual(r.guest.inputs[r.guest.cmds.index(tar[0])], "./Desktop/new.txt\n")
        self.assertTrue(any("capped" in f and "8 bytes" in f for f in r.r["fail_reasons"]))

    def test_first_file_above_the_cap_means_no_archive(self):
        with mock.patch.object(run.project, "HOME_CAP_BYTES", 2):
            r = self.collect()
        h = r.r["home_changes"]
        self.assertEqual((h["files"], h["bytes"], h["capped"], h["archive"]), (2, 8, True, None))
        self.assertFalse(any("home-changed.tgz" in c for c in r.guest.cmds))
        self.assertTrue(any("capped" in f for f in r.r["fail_reasons"]))

    def test_pull_failure_is_a_fail_reason(self):
        r = self.collect(pull_ok=False)
        self.assertIsNone(r.r["home_changes"]["archive"])
        self.assertTrue(any("home changed-files archive could not be pulled" in f for f in r.r["fail_reasons"]))

    def test_bad_names_after_the_run_are_a_fail_reason(self):
        r = self.collect(after=AFTER + "1 1 1 ./bad\nname\n\0")
        self.assertEqual(r.r["home_changes"]["bad_names"], 1)
        self.assertTrue(any("1 names refused" in f for f in r.r["fail_reasons"]))
        self.assertEqual(r.r["home_changes"]["added"], ["Desktop/new.txt"])

    def test_a_rerun_does_not_double_count_bad_names(self):
        """agy fix-round F1: salvage may rerun collect_home after an interrupt; bad_names is the before count
        plus this listing's, not a running sum."""
        r = self.collect(after=AFTER + "1 1 1 ./bad\nname\n\0")
        self.assertEqual(r.r["home_changes"]["bad_names"], 1)
        r.collect_home()
        self.assertEqual(r.r["home_changes"]["bad_names"], 1)
        self.assertEqual(sum("1 names refused" in f for f in r.r["fail_reasons"]), 2)

    def test_disabled_capture_runs_no_listing(self):
        r = self.collect(disabled=True)
        self.assertIsNone(r.r["home_changes"])
        self.assertFalse(any("lstat(" in c for c in r.guest.cmds))
        self.assertTrue(any(c.startswith("tar czf ~/results.tgz") for c in r.guest.cmds))


def project_listing(text):
    return run.project.parse_listing(text)[0]


class StubGuest:
    """Every command succeeds with no output, pulls fail and are listed, the ping answers as told and is counted."""
    def __init__(self, reachable=True):
        self.reachable, self.pings, self.pulled = reachable, 0, []

    def run(self, cmd, timeout=120, input=None):
        return done(0)

    def ok(self, cmd, timeout=30):
        self.pings += cmd == "true"
        return self.reachable

    def pull(self, remote, local):
        self.pulled.append(remote)
        return False


class SalvageTest(unittest.TestCase):
    """Lookout COMPUTERUSE-GUEST-OUTPUT-001 F1 and the agy fix-round review (fix round 1):
    an error or an interrupt after the agent is ready, before or during collection, still runs every output
    collector that has not completed before teardown deletes the clone; a completed collector is not run
    twice; the original failure stays first in the record; a normal run collects once."""
    def execute(self, agent_phase, ready=True, reachable=True, mode="scripted", collect=None, end=None, checks=(),
                **collectors):
        r = bare(pathlib.Path(tempfile.mkdtemp()))
        r.mode, order = mode, []
        r.cfg["checks"] = list(checks)
        for phase in ("preflight_host", "clone_boot", "start_proxy", "connect"):
            setattr(r, phase, lambda: None)
        r.run_probes = lambda when: True

        def start_session():
            if not ready:
                raise RuntimeError("agent cannot reach the API from tmux (reach check)")
            r.r["timings"]["t_ready"] = 1.0
            r.r["home_changes"] = {"added": None, "modified": None, "deleted": None, "archive": None,
                                   "files": None, "bytes": None, "capped": None, "bad_names": 0}
            r.home_before = {}
        r.start_session = start_session
        setattr(r, "experiment" if mode == "scripted" else "interactive", agent_phase)
        for name in ("collect_project", "collect_home", "archive_results"):
            f = collectors.get(name)  # a stand-in returns what the collector returns: True when it timed out
            setattr(r, name, (lambda n=name, f=f: (order.append(n), f())[1]) if f else (lambda n=name: order.append(n)))
        if collect != "real":  # "real": Run.collect / collect_interactive, which call collect_outputs
            r.collect = collect or (lambda: order.append("collect"))
        r.teardown = lambda: order.append("teardown")
        r.check_proxy_log = lambda: None
        r.guest = StubGuest(reachable)
        r.session = mock.Mock(stops=[], pane=lambda history=False: "")
        if end is not None:  # an exception, or a function of the session mock
            r.session.end.side_effect = end if isinstance(end, Exception) else (lambda: end(r.session))
        with mock.patch.object(run.transcript, "extract"):
            r.execute()
        return r, order, json.loads((r.rec_dir / "record.json").read_text())

    def test_error_after_ready_collects_before_teardown(self):
        def boom():
            raise RuntimeError("boom")
        r, order, rec = self.execute(boom)
        self.assertEqual(order, ["collect_project", "collect_home", "archive_results", "teardown"])
        self.assertEqual(rec["fail_reasons"][0], "error: boom")
        self.assertEqual((rec["end_reason"], rec["salvage"], rec["verdict"]), ("error", "collected", "FAIL"))
        self.assertIn("t_collect", rec["timings"])
        self.assertTrue(any(p["phase"] == "t_collect" and p.get("salvage") for p in rec["phases"]))
        r.session.end.assert_called_once_with()
        r.session.release.assert_called_once_with()

    def test_interrupt_after_ready_collects_before_teardown(self):
        def ctrl_c():
            raise KeyboardInterrupt()
        r, order, rec = self.execute(ctrl_c, mode="interactive")
        self.assertEqual(order, ["collect_project", "collect_home", "archive_results", "teardown"])
        self.assertEqual((rec["end_reason"], rec["salvage"]), ("operator_interrupt", "collected"))
        self.assertTrue(rec["fail_reasons"][0].startswith("interrupted"))

    def test_failure_before_ready_salvages_nothing(self):
        r, order, rec = self.execute(lambda: None, ready=False)
        self.assertEqual(order, ["teardown"])
        self.assertNotIn("salvage", rec)
        self.assertTrue(any("reach check" in f for f in rec["fail_reasons"]))

    def test_normal_run_collects_once(self):
        """Through the real Run.collect (scripted) and Run.collect_interactive."""
        for mode in ("scripted", "interactive"):
            r, order, rec = self.execute(lambda: None, mode=mode, collect="real")
            self.assertEqual(order, ["collect_project", "collect_home", "archive_results", "teardown"], mode)
            self.assertNotIn("salvage", rec)
            self.assertEqual(r.guest.pings, 0)
            self.assertIn("artefacts", rec)
            self.assertFalse(any("salvage" in f for f in rec["fail_reasons"]), mode)

    def test_interrupt_during_the_first_collector_resumes_collection(self):
        """agy fix-round F1: Ctrl-C while collect_project runs (signals are live until the finally block);
        nothing had completed, so salvage runs all three, the interrupted one from the top."""
        calls = []

        def project():
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt()
        r, order, rec = self.execute(lambda: None, collect="real", collect_project=project)
        self.assertEqual(order, ["collect_project", "collect_project", "collect_home", "archive_results", "teardown"])
        self.assertEqual((rec["end_reason"], rec["salvage"]), ("operator_interrupt", "collected"))

    def test_interrupt_during_a_later_collector_reruns_only_what_did_not_complete(self):
        calls = []

        def home():
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt()
        r, order, rec = self.execute(lambda: None, collect="real", collect_home=home)
        self.assertEqual(order, ["collect_project", "collect_home", "collect_home", "archive_results", "teardown"])
        self.assertEqual(rec["salvage"], "collected")

    def test_timeout_then_an_unreachable_guest_skips_the_rest(self):
        """agy fix-round F2 and fix-round 2 F1: after a collector timed out the guest is pinged before the next
        one; a wedged guest costs one timeout and a ping, not every timeout in turn with signals ignored. In
        normal collection (both modes) the record then says guest_unreachable with no t_collect, the salvage
        does not end the session or ping again, and the scripted check files are not pulled."""
        for mode in ("scripted", "interactive"):
            r, order, rec = self.execute(lambda: None, mode=mode, collect="real", reachable=False,
                                         collect_home=lambda: True, checks=[{"guest_file_equals": ["~/x.txt", "y"]}])
            self.assertEqual(order, ["collect_project", "collect_home", "teardown"], mode)
            self.assertTrue(any("guest unreachable after a collection timeout" in f and "archive_results" in f
                                for f in rec["fail_reasons"]), mode)
            self.assertEqual(sum("unreachable" in f for f in rec["fail_reasons"]), 1, mode)
            self.assertEqual((rec["salvage"], r.guest.pings), ("guest_unreachable", 1), mode)
            self.assertNotIn("~/x.txt", r.guest.pulled, mode)
            self.assertNotIn("t_collect", rec["timings"], mode)
            self.assertFalse(any(p["phase"] == "t_collect" for p in rec["phases"]), mode)
            r.session.end.assert_not_called()
            self.assertIn("artefacts", rec, mode)
            if mode == "scripted":
                self.assertEqual(rec["artefacts"]["checks"][0]["got"], None)

    def test_timeout_with_a_live_guest_goes_on(self):
        r, order, rec = self.execute(lambda: None, collect="real", collect_home=lambda: True,
                                     checks=[{"guest_file_equals": ["~/x.txt", "y"]}])
        self.assertEqual(order, ["collect_project", "collect_home", "archive_results", "teardown"])
        self.assertEqual(r.guest.pings, 1)
        self.assertIn("~/x.txt", r.guest.pulled)
        self.assertNotIn("salvage", rec)
        self.assertIn("t_collect", rec["timings"])

    def test_collector_error_in_salvage_is_a_fail_reason_and_teardown_runs(self):
        def boom():
            raise RuntimeError("boom")

        def gone():
            raise RuntimeError("tar gone")
        r, order, rec = self.execute(boom, collect_home=gone)
        self.assertEqual(order, ["collect_project", "collect_home", "teardown"])
        self.assertEqual(rec["fail_reasons"][0], "error: boom")
        self.assertTrue(any("salvage error: tar gone" in f for f in rec["fail_reasons"]))
        self.assertEqual(rec["salvage"], "error")

    def test_final_collector_timeout_then_a_dead_guest_pulls_no_check_file(self):
        """Lookout round 3 F1-R3: the results archive (the last collector) times out and the guest is gone;
        the ping after it fails, so the scripted check files are not pulled (got null); every collector ran,
        so t_collect is marked and nothing was skipped (no salvage field)."""
        r, order, rec = self.execute(lambda: None, collect="real", reachable=False, archive_results=lambda: True,
                                     checks=[{"guest_file_equals": ["~/x.txt", "y"]}])
        self.assertEqual(order, ["collect_project", "collect_home", "archive_results", "teardown"])
        self.assertEqual(r.guest.pings, 1)
        self.assertNotIn("~/x.txt", r.guest.pulled)
        self.assertEqual(rec["artefacts"]["checks"][0]["got"], None)
        self.assertIn("t_collect", rec["timings"])
        self.assertNotIn("salvage", rec)
        self.assertIn("guest unreachable after a collection timeout; every collector ran", rec["fail_reasons"])
        r.session.end.assert_not_called()

    def test_final_collector_timeout_with_a_live_guest_pulls_the_check_files(self):
        r, order, rec = self.execute(lambda: None, collect="real", archive_results=lambda: True,
                                     checks=[{"guest_file_equals": ["~/x.txt", "y"]}])
        self.assertEqual(order, ["collect_project", "collect_home", "archive_results", "teardown"])
        self.assertEqual(r.guest.pings, 1)
        self.assertIn("~/x.txt", r.guest.pulled)
        self.assertIn("t_collect", rec["timings"])
        self.assertNotIn("salvage", rec)
        self.assertFalse(any("unreachable" in f for f in rec["fail_reasons"]))

    def test_final_collector_timeout_inside_the_salvage_is_still_collected(self):
        """Every collector ran; the results timeout and the failed ping after it are fail reasons, the salvage
        itself completed."""
        def boom():
            raise RuntimeError("boom")
        r, order, rec = self.execute(boom, reachable=False, end=lambda s: {"rc": 0}, archive_results=lambda: True)
        self.assertEqual(order, ["collect_project", "collect_home", "archive_results", "teardown"])
        self.assertEqual((rec["fail_reasons"][0], rec["salvage"], r.guest.pings), ("error: boom", "collected", 1))
        self.assertTrue(any(p["phase"] == "t_collect" and p.get("salvage") for p in rec["phases"]))
        self.assertIn("guest unreachable after a collection timeout; every collector ran", rec["fail_reasons"])

    def test_dead_guest_during_salvage_is_not_recorded_as_collected(self):
        """Lookout round 2 F1-R2: the session end ran (rc 0), a collector then timed out and the ping before
        the next one failed; the skipped collectors are named and the record must not claim a completed
        salvage (no `collected`, no t_collect)."""
        def boom():
            raise RuntimeError("boom")
        r, order, rec = self.execute(boom, reachable=False, end=lambda s: {"rc": 0}, collect_home=lambda: True)
        self.assertEqual(order, ["collect_project", "collect_home", "teardown"])
        self.assertEqual(rec["fail_reasons"][0], "error: boom")
        self.assertEqual(rec["salvage"], "guest_unreachable")
        self.assertTrue(any("not collected: archive_results" in f for f in rec["fail_reasons"]))
        self.assertEqual(sum("unreachable" in f for f in rec["fail_reasons"]), 1)
        self.assertNotIn("t_collect", rec["timings"])
        self.assertFalse(any(p["phase"] == "t_collect" for p in rec["phases"]))
        self.assertEqual(r.guest.pings, 1)

    def test_unreachable_guest_is_a_fail_reason_and_teardown_follows(self):
        def boom():
            raise RuntimeError("boom")
        r, order, rec = self.execute(boom, reachable=False)
        self.assertEqual(order, ["teardown"])
        self.assertEqual(rec["salvage"], "guest_unreachable")
        self.assertEqual(rec["fail_reasons"][0], "error: boom")
        self.assertIn("salvage: guest unreachable; not collected: collect_project, collect_home, archive_results",
                      rec["fail_reasons"])

    def test_unreachable_guest_after_a_partial_collection_names_what_is_missing(self):
        """agy fix-round 2 F2: an interrupt cut collect_home short after collect_project completed and the
        guest does not answer when the salvage starts; the fail reason names the two collectors left."""
        def ctrl_c():
            raise KeyboardInterrupt()
        r, order, rec = self.execute(lambda: None, collect="real", reachable=False, collect_home=ctrl_c)
        self.assertEqual(order, ["collect_project", "collect_home", "teardown"])
        self.assertEqual((rec["end_reason"], rec["salvage"]), ("operator_interrupt", "guest_unreachable"))
        self.assertIn("salvage: guest unreachable; not collected: collect_home, archive_results", rec["fail_reasons"])
        self.assertNotIn("t_collect", rec["timings"])

    def test_a_session_end_that_ran_proves_the_guest_reachable(self):
        """agy fix-round F2: Session.end just ran a command in the guest; a second single-shot ping was a
        loss path of its own when ssh hiccuped."""
        def boom():
            raise RuntimeError("boom")
        r, order, rec = self.execute(boom, reachable=False, end=lambda s: {"rc": 0})
        self.assertEqual(order, ["collect_project", "collect_home", "archive_results", "teardown"])
        self.assertEqual((rec["salvage"], r.guest.pings), ("collected", 0))

    def test_session_released_even_when_end_raises(self):
        def boom():
            raise RuntimeError("boom")
        r, order, rec = self.execute(boom, end=RuntimeError("ssh died"))
        r.session.release.assert_called_once_with()
        self.assertEqual(rec["salvage"], "collected")

    def test_salvage_stop_is_in_session_stops(self):
        def ctrl_c():
            raise KeyboardInterrupt()
        def end(s):
            s.stops.append({"at": "t", "rc": 0})
            return s.stops[-1]
        r, order, rec = self.execute(ctrl_c, mode="interactive", end=end)
        self.assertEqual(rec["session_stops"], [{"at": "t", "rc": 0}])
