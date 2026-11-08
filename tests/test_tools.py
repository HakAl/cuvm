"""Guest tools (the guest-tools design): the per-run settings file, the claude flags, the judge's allowed
names, and the install step of Run.connect, with every guest command stubbed."""
import hashlib
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from cuvm import run, tools
from tests.test_run_lifecycle import bare, done

BASE = json.loads(tools.BASE.read_text())
BASE_SHA = hashlib.sha256(tools.BASE.read_bytes()).hexdigest()


class SettingsTest(unittest.TestCase):
    def test_tools_move_from_deny_to_allow_and_nothing_else_changes(self):
        for tl in (["Bash"], ["Edit", "Bash"], list(tools.TOOLS)):
            text = tools.settings(tl)
            self.assertTrue(text.endswith("\n"))
            got = json.loads(text)
            self.assertEqual(got["permissions"]["deny"], [d for d in BASE["permissions"]["deny"] if d not in tl], tl)
            self.assertEqual(got["permissions"]["allow"],
                             BASE["permissions"]["allow"] + [t for t in tools.TOOLS if t in tl], tl)  # TOOLS order
            strip = lambda d: {k: v for k, v in d.items() if k != "permissions"}
            self.assertEqual(strip(got), strip(BASE))
            perms = lambda d: {k: v for k, v in d["permissions"].items() if k not in ("allow", "deny")}
            self.assertEqual(perms(got), perms(BASE))
            self.assertEqual(got["permissions"]["defaultMode"], "dontAsk")

    def test_names_outside_the_set_are_refused(self):
        for bad in (["WebFetch"], ["Bash", "Agent"], [], ["bash"]):
            with self.assertRaises(ValueError, msg=bad):
                tools.settings(bad)

    def test_claude_args_and_allowed_names(self):
        self.assertEqual(tools.claude_args({"agent_args": []}), [])
        self.assertEqual(tools.claude_args({"tools": ["Bash", "Edit"]}), ["--tools", "ToolSearch,Bash,Edit"])
        self.assertEqual(tools.allowed_names({}), ("ToolSearch",))
        self.assertEqual(tools.allowed_names({"tools": ["Bash", "Edit"]}), ("ToolSearch", "Bash", "Edit"))


class ToolsGuest:
    """Records every command and its stdin; shasum answers with the hash of the last text written, or a
    fixed hash when `readback` is set; `cat >` exits with `write_rc`."""
    def __init__(self, write_rc=0, readback=None):
        self.cmds, self.write_rc, self.readback, self.written = [], write_rc, readback, None

    def run(self, cmd, input=None, timeout=120):
        self.cmds.append((cmd, input, timeout))
        if cmd.startswith("cat > ~/.claude/settings.json"):
            self.written = input
            return done(self.write_rc)
        if cmd.startswith("shasum"):
            h = self.readback or (hashlib.sha256(self.written.encode()).hexdigest() if self.written else BASE_SHA)
            return done(0, "%s  settings.json\n" % h)
        return done(0)

    def ok(self, cmd, timeout=30):
        return False


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.r = bare(pathlib.Path(tempfile.mkdtemp()))
        self.r.rec_dir.mkdir(exist_ok=True)
        self.r.cfg["tools"] = ["Bash", "Edit"]
        self.r.image["settings_sha256"] = BASE_SHA
        self.r.r["guest"] = {"settings_sha256": BASE_SHA}

    def test_installs_the_derived_file_and_records_both_hashes(self):
        g = self.r.guest = ToolsGuest()
        self.r.install_tools()
        text = tools.settings(["Bash", "Edit"])
        self.assertEqual(g.written, text)
        cmd, inp, timeout = [c for c in g.cmds if c[0].startswith("cat >")][0]
        self.assertEqual((cmd, timeout), ("cat > ~/.claude/settings.json", 30))
        self.assertEqual(self.r.r["tools"], ["Bash", "Edit"])
        self.assertEqual(self.r.r["guest"]["settings_baseline_sha256"], BASE_SHA)
        self.assertEqual(self.r.r["guest"]["settings_sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertNotEqual(self.r.r["guest"]["settings_sha256"], BASE_SHA)
        self.assertIn("tools: settings installed allowing ['Bash', 'Edit']", (self.r.rec_dir / "run.log").read_text())

    def test_an_image_with_other_settings_is_refused_before_any_write(self):
        g = self.r.guest = ToolsGuest()
        self.r.image["settings_sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "cannot derive"):
            self.r.install_tools()
        self.assertIsNone(g.written)
        self.assertEqual(self.r.r["tools"], ["Bash", "Edit"])  # the attempt is on record
        self.assertEqual(self.r.r["guest"]["settings_baseline_sha256"], "0" * 64)
        self.assertEqual(self.r.r["guest"]["settings_sha256"], BASE_SHA)  # nothing was written

    def test_a_failed_write_raises_with_no_stale_hash(self):
        self.r.guest = ToolsGuest(write_rc=1)
        with self.assertRaisesRegex(RuntimeError, "could not install"):
            self.r.install_tools()
        self.assertIsNone(self.r.r["guest"]["settings_sha256"])
        self.assertEqual(self.r.r["tools"], ["Bash", "Edit"])

    def test_a_readback_that_differs_raises_and_records_what_was_read(self):
        self.r.guest = ToolsGuest(readback="f" * 64)
        with self.assertRaisesRegex(RuntimeError, "differ after install"):
            self.r.install_tools()
        self.assertEqual(self.r.r["guest"]["settings_sha256"], "f" * 64)


class ConnectTest(unittest.TestCase):
    """connect runs install_tools right after the baseline check and before the proxy-port edit, and only
    when `tools` is configured."""
    def setUp(self):
        self.r = bare(pathlib.Path(tempfile.mkdtemp()))
        self.r.rec_dir.mkdir(exist_ok=True)
        self.r.image.update(settings_sha256=BASE_SHA, guest_user="admin")
        self.r.image_path = self.r.out / "img.json"
        self.r.ip, self.r.port = "192.0.2.9", 1

    def connect(self):
        g = ToolsGuest()
        g.wait_ssh = lambda t: True
        g.host_key_fp = lambda: "SHA256:x"
        with mock.patch.object(run, "Guest", return_value=g), \
             mock.patch.object(run.imgmod, "pin_fp", return_value=("SHA256:x", "matched")), \
             mock.patch.object(self.r, "install_tools", wraps=self.r.install_tools) as inst:
            with self.assertRaisesRegex(RuntimeError, "proxy port"):  # ok() is False: connect stops right after
                self.r.connect()
        return g, inst

    def test_called_once_with_tools(self):
        self.r.cfg["tools"] = ["Bash"]
        g, inst = self.connect()
        self.assertEqual(inst.call_count, 1)
        self.assertEqual(g.written, tools.settings(["Bash"]))
        self.assertEqual(self.r.r["guest"]["settings_baseline_sha256"], BASE_SHA)
        self.assertEqual(self.r.r["guest"]["settings_sha256"], hashlib.sha256(g.written.encode()).hexdigest())

    def test_not_called_without_tools(self):
        g, inst = self.connect()
        inst.assert_not_called()
        self.assertIsNone(g.written)
        self.assertEqual(self.r.r["guest"]["settings_sha256"], BASE_SHA)
        self.assertNotIn("settings_baseline_sha256", self.r.r["guest"])
        self.assertNotIn("tools", self.r.r)


class StartArgsTest(unittest.TestCase):
    """start_session passes the config's args, then the tools flag, then the browser's flags."""
    def test_tools_flag_between_config_and_browser_args(self):
        import hashlib as h
        r = bare(pathlib.Path(tempfile.mkdtemp()))
        r.rec_dir.mkdir(exist_ok=True)
        r.dummy = "sk-ant-oat01-cuvm-dummy-" + "ab" * 24
        r.cfg.update(plan="max", agent_args=["--model", "x"], tools=["Bash", "Edit"], browser="webkit")
        r.image.update(guest_user="admin", browser={"node": "/n", "entry": "/e", "entry_sha256": "e" * 64,
                                                    "engines": {"webkit": "1"}})
        r.gw, r.port = "192.0.2.1", 1
        from cuvm import browser
        tail = browser.claude_args(r.cfg, r.image)
        self.assertTrue(tail and tail[0] == "--mcp-config")  # the browser's flags are non-empty, so order is tested
        s = mock.Mock()
        s.inject.return_value = done(0, "")
        s.e3.return_value = done(0, h.sha256(r.dummy.encode()).hexdigest() + "\n")
        s.start.return_value = done(1)
        with mock.patch.object(run, "Session", return_value=s):
            with self.assertRaisesRegex(RuntimeError, "session start"):
                r.start_session()
        self.assertEqual(s.start.call_args.args[0], ["--model", "x", "--tools", "ToolSearch,Bash,Edit", *tail])
        r.cfg.pop("tools")
        with mock.patch.object(run, "Session", return_value=s):
            with self.assertRaisesRegex(RuntimeError, "session start"):
                r.start_session()
        self.assertEqual(s.start.call_args.args[0], ["--model", "x", *tail])


if __name__ == "__main__":
    unittest.main()
