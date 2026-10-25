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


def done(rc=0, out=""):
    return subprocess.CompletedProcess([], rc, out, "")


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
    r.keep_on_fail, r.t0, r.ip = False, time.monotonic(), None
    r.tart_proc = r.proxy_proc = r.guest = r.session = None
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
    def __init__(self, tar_rc=0, pull_ok=True):
        self.cmds, self.tar_rc, self.pull_ok = [], tar_rc, pull_ok

    def run(self, cmd, timeout=120, input=None):
        self.cmds.append(cmd)
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
