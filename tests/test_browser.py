import json
import pathlib
import tempfile
import unittest

from cuvm import browser, config, transcript

ROOT = pathlib.Path(__file__).resolve().parent.parent
IMAGE = {"guest_user": "admin", "browser": {
    "node": "/opt/homebrew/bin/node", "entry": "/opt/homebrew/lib/node_modules/@playwright/mcp/cli.js",
    "entry_sha256": "e" * 64, "engines": {"webkit": "2215"}}}


def cfg(**kw):
    c = {"agent_args": ["--tools", "ToolSearch", "--disallowedTools", "Bash"], "results": "~/exp/results"}
    c.update(kw)
    return c


class ArgsTest(unittest.TestCase):
    def test_no_browser_no_args(self):
        self.assertEqual(browser.claude_args(cfg(), IMAGE), [])

    def test_browser_args(self):
        a = browser.claude_args(cfg(browser="webkit"), IMAGE)
        self.assertEqual(a[:2], ["--mcp-config", "/Users/admin/.cuvm/browser-mcp.json"])
        self.assertEqual(a[a.index("--allowedTools") + 1], "mcp__playwright")
        denied = a[a.index("--disallowedTools") + 1].split(",")
        self.assertEqual(sorted(denied), sorted(browser.DENIED))
        self.assertIn("mcp__playwright__browser_run_code_unsafe", denied)
        self.assertNotIn("--strict-mcp-config", a)

    def test_mcp_config(self):
        m = browser.mcp_config(cfg(browser="webkit"), IMAGE, "192.0.2.1", 4321)
        s = m["mcpServers"]["playwright"]
        self.assertEqual(s["command"], IMAGE["browser"]["node"])
        a = s["args"]
        self.assertEqual(a[0], IMAGE["browser"]["entry"])
        self.assertEqual(a[a.index("--browser") + 1], "webkit")
        self.assertEqual(a[a.index("--proxy-server") + 1], "http://192.0.2.1:4321")
        self.assertEqual(a[a.index("--output-dir") + 1], "/Users/admin/exp/results/browser")
        for f in ("--isolated", "--no-webmcp"):
            self.assertIn(f, a)
        for f in ("--ignore-https-errors", "--allow-unrestricted-file-access", "--caps", "--headless",
                  "--extension", "--cdp-endpoint", "--no-sandbox"):
            self.assertNotIn(f, a)
        self.assertNotIn("~", json.dumps(m))

    def test_headless(self):
        a = browser.mcp_config(cfg(browser="webkit", browser_headless=True), IMAGE, "192.0.2.1", 1)["mcpServers"]["playwright"]["args"]
        self.assertIn("--headless", a)

    def test_rules(self):
        self.assertEqual(browser.tool_rules(cfg()), (("mcp__computer-use__",), ("ToolSearch",), ()))
        p, n, d = browser.tool_rules(cfg(browser="webkit"))
        self.assertEqual((p, n), (("mcp__computer-use__", "mcp__playwright__"), ("ToolSearch",)))
        self.assertEqual(set(d), set(browser.DENIED))

    def test_image_check(self):
        browser.check_image(cfg(), {})  # no browser: nothing required
        browser.check_image(cfg(browser="webkit"), IMAGE)
        with self.assertRaises(config.ConfigError):
            browser.check_image(cfg(browser="chromium"), IMAGE)
        with self.assertRaises(config.ConfigError):
            browser.check_image(cfg(browser="webkit"), {"guest_user": "admin"})


class ConfigKeysTest(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        (self.dir / "inputs").mkdir()
        (self.dir / "inputs/known.txt").write_text("k\n")
        self.base = json.loads((ROOT / "experiments/smoke/config.json").read_text())

    def load(self, **kw):
        d = dict(self.base, **kw)
        p = self.dir / "config.json"
        p.write_text(json.dumps(d))
        return config.load(p)[0]

    def test_browser_values(self):
        self.assertEqual(self.load(browser="webkit")["browser"], "webkit")
        for bad in ("safari", 1, ""):
            with self.assertRaises(config.ConfigError):
                self.load(browser=bad)

    def test_headless_needs_browser_and_bool(self):
        self.assertTrue(self.load(browser="webkit", browser_headless=True)["browser_headless"])
        with self.assertRaises(config.ConfigError):
            self.load(browser_headless=True)
        with self.assertRaises(config.ConfigError):
            self.load(browser="webkit", browser_headless="yes")

    def test_check_kinds(self):
        self.load(browser="webkit", checks=[{"browser_navigated": "example.com"}])
        with self.assertRaises(config.ConfigError):
            self.load(checks=[{"browser_navigated": "example.com"}])  # needs browser
        with self.assertRaises(config.ConfigError):
            self.load(checks=[{"file_contains": ["a", "b"]}])
        with self.assertRaises(config.ConfigError):
            self.load(checks=[{"guest_file_equals": "x"}])
        for bad in ("https://example.org", "Example.org", "", "example.org:443", "example.org/x"):
            with self.assertRaises(config.ConfigError, msg=bad):
                self.load(browser="webkit", checks=[{"browser_navigated": bad}])


def use(i, name, inp=None):
    return json.dumps({"message": {"content": [{"type": "tool_use", "id": i, "name": name, "input": inp or {}}]}})


def res(i, err=False, text="ok"):
    return json.dumps({"message": {"content": [{"type": "tool_result", "tool_use_id": i, "is_error": err,
                                                "content": [{"type": "text", "text": text}]}]}})


NAV = "mcp__playwright__browser_navigate"
TYPE = "mcp__computer-use__type"


def ex(lines):
    return transcript.extract(lines, *browser.tool_rules(cfg(browser="webkit")))


class TranscriptTest(unittest.TestCase):
    def test_navigate_before_input(self):
        r = ex([use("a", NAV, {"url": "https://example.com/"}), res("a"), use("b", TYPE), res("b")])
        self.assertEqual(r["browser_navigated_hosts"], ["example.com"])
        self.assertEqual(r["browser_hosts_before_input"], ["example.com"])
        self.assertEqual(r["forbidden_tools_attempted"], [])

    def test_navigate_after_input_does_not_count(self):
        r = ex([use("b", TYPE), res("b"), use("a", NAV, {"url": "https://example.com/"}), res("a")])
        self.assertEqual(r["browser_hosts_before_input"], [])

    def test_failed_navigate_does_not_count(self):
        r = ex([use("a", NAV, {"url": "https://example.com/"}), res("a", err=True), use("b", TYPE), res("b")])
        self.assertEqual((r["browser_navigated_hosts"], r["browser_hosts_before_input"]), ([], []))

    def test_denied_names_are_forbidden_despite_prefix(self):
        r = ex([use("a", "mcp__playwright__browser_run_code_unsafe"), res("a")])
        self.assertEqual(r["forbidden_tools_used"], ["mcp__playwright__browser_run_code_unsafe"])

    def test_playwright_forbidden_without_browser(self):
        r = transcript.extract([use("a", NAV, {"url": "https://example.com"}), res("a")], *browser.tool_rules(cfg()))
        self.assertEqual(r["forbidden_tools_used"], [NAV])


if __name__ == "__main__":
    unittest.main()


from unittest import mock  # noqa: E402

from cuvm import run, session  # noqa: E402
from tests.test_run_lifecycle import FakeGuest, bare, done  # noqa: E402


class McpCheckTest(unittest.TestCase):
    def check(self, panes, names):
        g = mock.Mock()
        s = session.Session(g, "192.0.2.1", 1)
        it = iter(panes)
        last = [panes[-1]]
        def pane(history=False):
            last[0] = next(it, last[0])
            return last[0]
        with mock.patch.object(s, "pane", side_effect=pane), mock.patch.object(s, "keys"), \
                mock.patch.object(session.time, "sleep"):
            return s.mcp_check(names)

    def test_waits_for_every_server(self):
        panes = ["computer-use ✔ connected\nplaywright … connecting", "computer-use ✔ connected\nplaywright ✔ connected"]
        st, _ = self.check(panes, ("computer-use", "playwright"))
        self.assertEqual(st, {"computer-use": True, "playwright": True})

    def test_server_that_never_connects_fails(self):
        st, _ = self.check(["computer-use ✔ connected\nplaywright ✘ failed"], ("computer-use", "playwright"))
        self.assertEqual(st, {"computer-use": True, "playwright": False})

    def test_default_is_computer_use_only(self):
        st, _ = self.check(["computer-use ✔ connected"], ("computer-use",))
        self.assertEqual(st, {"computer-use": True})


class BrowserGuest(FakeGuest):
    """FakeGuest that remembers files written with cat > and answers shasum for them."""
    def __init__(self, entry_sha="e" * 64, **kw):
        super().__init__(**kw)
        self.files, self.entry_sha = {}, entry_sha

    def run(self, cmd, timeout=120, input=None):
        self.cmds.append(cmd)
        if cmd.startswith("mkdir -p ~/.cuvm") and "cat >" in cmd:
            self.files[cmd.rsplit("cat > ", 1)[1].strip()] = input
            return done()
        if cmd.startswith("shasum -a 256"):
            import hashlib
            out = []
            for p in cmd.split()[3:]:
                p = p.strip("'")
                if p in self.files:
                    out.append("%s  %s" % (hashlib.sha256(self.files[p].encode()).hexdigest(), p))
                elif p == IMAGE["browser"]["entry"]:
                    out.append("%s  %s" % (self.entry_sha, p))
            return done(0, "\n".join(out) + "\n")
        return super().run(cmd, timeout, input)


class RunBrowserTest(unittest.TestCase):
    def setUp(self):
        self.r = bare(pathlib.Path(tempfile.mkdtemp()))
        self.r.cfg.update(browser="webkit", agent_args=["--tools", "ToolSearch"])
        self.r.image = dict(IMAGE, image_sha256=self.r.image["image_sha256"])
        self.r.gw, self.r.port = "192.0.2.1", 4321

    def test_install_writes_config_and_records_it(self):
        self.r.guest = BrowserGuest()
        self.r.install_browser()
        path = browser.config_path(IMAGE)
        self.assertEqual(json.loads(self.r.guest.files[path]), browser.mcp_config(self.r.cfg, IMAGE, "192.0.2.1", 4321))
        self.assertEqual(self.r.r["browser"]["engine"], "webkit")
        self.assertEqual(self.r.browser_probe()["pass"], True)

    def test_probe_fails_on_entry_hash_mismatch(self):
        self.r.guest = BrowserGuest(entry_sha="0" * 64)
        self.r.install_browser()
        b1 = self.r.browser_probe()
        self.assertFalse(b1["pass"])

    def test_session_gets_browser_args_and_waits_for_playwright(self):
        import hashlib
        self.r.cfg["plan"] = "max"
        self.r.dummy = "sk-ant-oat01-cuvm-dummy-" + "ab" * 24
        s = mock.Mock()
        s.inject.return_value = done(0, "")
        s.e3.return_value = done(0, hashlib.sha256(self.r.dummy.encode()).hexdigest() + "\n")
        s.start.return_value = done(0)
        s.wait_ready.return_value = (True, "")
        s.mcp_check.return_value = ({"computer-use": True, "playwright": False}, "")
        self.r.guest = BrowserGuest()
        self.r.r["guest"] = {}
        with mock.patch.object(run, "Session", return_value=s):
            with self.assertRaisesRegex(RuntimeError, "playwright"):
                self.r.start_session()
        self.assertEqual(s.start.call_args.args[0], ["--tools", "ToolSearch"] + browser.claude_args(self.r.cfg, IMAGE))
        self.assertEqual(s.mcp_check.call_args.args[0], ("computer-use", "playwright"))

    def test_navigated_check(self):
        r = self.r
        tdir = r.res_dir / "transcripts"
        tdir.mkdir(parents=True)
        lines = [use("a", NAV, {"url": "https://example.com/"}), res("a"), use("b", TYPE), res("b", text=r.id)]

        class G(FakeGuest):
            def run(self, cmd, timeout=120, input=None):
                self.cmds.append(cmd)
                return done(0, "/Users/admin/.claude/projects/p/t.jsonl\n") if "find -L" in cmd else done()

            def pull(self, remote, local):
                if remote.endswith("t.jsonl"):
                    pathlib.Path(local).parent.mkdir(parents=True, exist_ok=True)
                    pathlib.Path(local).write_text("\n".join(lines) + "\n")
                    return True
                return remote == "results.tgz"
        for want, ok in (("example.com", True), ("example.org", False)):
            r.r["fail_reasons"] = []
            r.guest, r.session = G(), mock.Mock(pane=lambda history=False: "")
            r.cfg["checks"] = [{"browser_navigated": want}]
            r.collect()
            got = r.r["artefacts"]["checks"][0]
            self.assertEqual(got["pass"], ok, got)
            self.assertEqual(any("browser_navigated" in f for f in r.r["fail_reasons"]), not ok)
