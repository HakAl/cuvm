import json
import pathlib
import tempfile
import unittest

from cuvm import config

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = pathlib.Path(__file__).parent / "fixtures"


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        (self.dir / "inputs").mkdir()
        (self.dir / "inputs/known.txt").write_text("known\n")

    def write(self, d):
        path = self.dir / "config.json"
        path.write_text(json.dumps(d))
        return path

    def base(self):
        return json.loads((ROOT / "experiments/smoke/config.json").read_text())

    def test_smoke_config_valid(self):
        c, h = config.load(ROOT / "experiments/smoke/config.json")
        self.assertEqual(len(h), 64)
        self.assertEqual(c["apps"], ["TextEdit"])
        self.assertTrue(pathlib.Path(c["inputs"]).is_absolute())
        self.assertTrue(c["net_allow"].endswith("cuvm/data/allow-run.txt"))

    def test_paths_resolve_against_config_dir(self):
        d = self.base(); d["net_allow"] = "allow.txt"
        c, _ = config.load(self.write(d))
        self.assertEqual(pathlib.Path(c["inputs"]), self.dir.resolve() / "inputs")
        self.assertEqual(pathlib.Path(c["net_allow"]), self.dir.resolve() / "allow.txt")

    def test_rejects_inputs_without_known_txt(self):
        (self.dir / "inputs/known.txt").unlink()
        with self.assertRaises(config.ConfigError):
            config.load(self.write(self.base()))

    def test_rejects_missing_key(self):
        d = self.base(); del d["apps"]
        with self.assertRaises(config.ConfigError):
            config.load(self.write(d))

    def test_rejects_unknown_key(self):
        d = self.base(); d["inputs_rw"] = True
        with self.assertRaises(config.ConfigError):
            config.load(self.write(d))

    def test_rejects_bad_types(self):
        d = self.base(); d["allow_fullscreen"] = "no"
        with self.assertRaises(config.ConfigError):
            config.load(self.write(d))

    def test_on_refusal(self):
        d = self.base(); d["on_refusal"] = "deny"; d["refusal_reprompt"] = "Carry on, {run_id}."
        c, _ = config.load(self.write(d))
        self.assertEqual((c["on_refusal"], c["refusal_reprompt"]), ("deny", "Carry on, {run_id}."))
        for bad in ({"on_refusal": "ignore"}, {"refusal_reprompt": "x"}, {"on_refusal": "stop", "refusal_reprompt": "x"},
                    {"on_refusal": "deny", "refusal_reprompt": "  "}, {"on_refusal": True}):
            d = self.base(); d.update(bad)
            with self.assertRaises(config.ConfigError, msg=bad):
                config.load(self.write(d))

    def test_on_refusal_allowed_and_ignored_in_interactive(self):
        d = self.base(); d["on_refusal"] = "deny"; d["refusal_reprompt"] = "x"
        c, _ = config.load(self.write(d), interactive=True)
        self.assertIn("on_refusal", config.SCRIPTED_ONLY)
        self.assertIn("refusal_reprompt", config.SCRIPTED_ONLY)


if __name__ == "__main__":
    unittest.main()


class WebDevConfigTest(unittest.TestCase):
    """`project` and `dev_server` (the web-dev design); archives are built here, never read from experiments/."""
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        (self.dir / "inputs").mkdir()
        (self.dir / "inputs/known.txt").write_text("known\n")
        import tarfile, io
        with tarfile.open(self.dir / "inputs/project.tgz", "w:gz") as t:
            ti = tarfile.TarInfo("app/index.html"); ti.size = 1
            t.addfile(ti, io.BytesIO(b"x"))

    def write(self, **extra):
        d = json.loads((ROOT / "experiments/smoke/config.json").read_text())
        d.update(extra)
        path = self.dir / "config.json"
        path.write_text(json.dumps(d))
        return path

    def dev(self, **kw):
        d = {"command": "npm run dev -- --host 127.0.0.1 --port 5173 --strictPort", "port": 5173}
        d.update(kw)
        return d

    def test_project_alone(self):
        c, _ = config.load(self.write(project="project.tgz"))
        self.assertEqual(c["project_top"], "app")

    def test_dev_server_full(self):
        c, _ = config.load(self.write(project="project.tgz", browser="webkit", dev_server=self.dev()))
        self.assertEqual(c["dev_server"]["ready_s"], 60)

    def test_dev_server_needs_project_and_browser(self):
        with self.assertRaises(config.ConfigError):
            config.load(self.write(browser="webkit", dev_server=self.dev()))
        with self.assertRaises(config.ConfigError):
            config.load(self.write(project="project.tgz", dev_server=self.dev()))

    def test_bad_dev_server_values(self):
        for bad in (self.dev(port=80), self.dev(port=True), self.dev(port="5173"), self.dev(command=""), self.dev(ready_s=False),
                    self.dev(extra=1), {"port": 5173}):
            with self.assertRaises(config.ConfigError, msg=bad):
                config.load(self.write(project="project.tgz", browser="webkit", dev_server=bad))

    def test_bad_project_names_and_missing_archive(self):
        for bad in ("../x.tgz", "a b.tgz", "project.zip", "missing.tgz", ""):
            with self.assertRaises(config.ConfigError, msg=bad):
                config.load(self.write(project=bad))

    def test_results_under_the_project_dir_refused(self):
        for bad in ("~/exp/project", "~/exp/project/app/out"):
            with self.assertRaises(config.ConfigError, msg=bad):
                config.load(self.write(project="project.tgz", results=bad))
        config.load(self.write(project="project.tgz", results="~/exp/projects"))

    def test_bad_archive_refused_at_load(self):
        import tarfile, io
        with tarfile.open(self.dir / "inputs/project.tgz", "w:gz") as t:
            ti = tarfile.TarInfo("app/esc"); ti.type = tarfile.SYMTYPE; ti.linkname = "../../x"
            t.addfile(ti)
        with self.assertRaises(config.ConfigError):
            config.load(self.write(project="project.tgz"))


class ToolsConfigTest(unittest.TestCase):
    """`tools` (the guest-tools design); the web-dev configs are loaded from copies with a synthesised archive."""
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        (self.dir / "inputs").mkdir()
        (self.dir / "inputs/known.txt").write_text("known\n")

    def write(self, **extra):
        d = json.loads((ROOT / "experiments/smoke/config.json").read_text())
        d["agent_args"] = ["--model", "claude-haiku-5-5"]
        d.update(extra)
        path = self.dir / "config.json"
        path.write_text(json.dumps(d))
        return path

    def test_tools_accepted_scripted_and_interactive(self):
        for interactive in (False, True):
            c, _ = config.load(self.write(tools=["Bash", "Edit"]), interactive=interactive)
            self.assertEqual(c["tools"], ["Bash", "Edit"])
        c, _ = config.load(self.write())
        self.assertNotIn("tools", c)

    def test_bad_tools_refused(self):
        for bad in ([], ["WebFetch"], ["Bash", "Bash"], "Bash", [1], ["bash"]):
            with self.assertRaises(config.ConfigError, msg=repr(bad)):
                config.load(self.write(tools=bad))

    def test_agent_args_must_be_strings(self):
        with self.assertRaises(config.ConfigError):
            config.load(self.write(agent_args=["--model", 3]))

    def test_tool_flags_in_agent_args_refused_when_tools_is_set(self):
        for flag in ("--tools", "--disallowedTools", "--disallowed-tools", "--allowedTools", "--allowed-tools"):
            for form in ([flag, "ToolSearch"], [flag + "=ToolSearch"]):
                with self.assertRaises(config.ConfigError, msg=form):
                    config.load(self.write(tools=["Bash"], agent_args=["--model", "x", *form]))
        config.load(self.write(agent_args=["--tools", "ToolSearch"]))  # without tools: the config's own business

    def test_agent_args_outside_the_allowlist_refused_when_tools_is_set(self):
        """With tools, agent_args is an allowlist (Lookout rounds 1 to 3: a name-based denylist missed settings
        sources, plan mode, custom agents and plugin hooks). Every form below validated at some point."""
        forms = (["--settings", '{"permissions":{"deny":["Bash"]}}'], ["--settings=deny.json"],
                 ["--setting-sources", "project"], ["--restricted"],
                 ["--managed-settings", '{"permissions":{"deny":["Bash"]}}'], ["--project-config-root=/x"],
                 ["--permission-mode", "plan"], ["--permission-mode=plan"], ["--inherit-permission-mode", "plan"],
                 ["--agents", '{"a":{"description":"d","prompt":"p","disallowedTools":["Bash"]}}', "--agent", "a"],
                 ["--plugin-dir", "/Users/admin/exp/project/top/plugin"], ["--plugin-dir-no-mcp=/x"],
                 ["--mcp-config", "x.json"], ["--foo"], ["positional"], ["-p", "hi"])
        for form in forms:
            with self.assertRaises(config.ConfigError, msg=form):
                config.load(self.write(tools=["Bash"], agent_args=["--model", "x", *form]))
        config.load(self.write(agent_args=["--restricted", "--settings", "x.json"]))  # without tools: the config's own business

    def test_allowlisted_agent_args_take_one_value(self):
        for ok in (["--model", "claude-haiku-5-5"], ["--model=claude-haiku-5-5", "--effort", "low"],
                   ["--effort=max", "--fallback-model", "claude-sonnet-5-5"], []):
            c, _ = config.load(self.write(tools=["Bash"], agent_args=ok))
            self.assertEqual(c["agent_args"], ok)
        for bad in (["--model"], ["--model="], ["--model", ""], ["--model", "--effort", "low"], ["--model", "-x"],
                    ["--model", "x", "y"], ["--effort", "low", "--model"]):
            with self.assertRaises(config.ConfigError, msg=bad):
                config.load(self.write(tools=["Bash"], agent_args=bad))

    def test_web_dev_tool_configs_load_from_copies(self):
        import tarfile, io
        with tarfile.open(self.dir / "inputs/project.tgz", "w:gz") as t:
            ti = tarfile.TarInfo("fixture/index.html"); ti.size = 1
            t.addfile(ti, io.BytesIO(b"x"))
        (self.dir / "allow.txt").write_text("api.anthropic.com\n")
        for name, modes in (("dev.json", (False, True)), ("shell.json", (False,))):
            (self.dir / "config.json").write_text((ROOT / "experiments/web-dev" / name).read_text())
            for interactive in modes:
                c, _ = config.load(self.dir / "config.json", interactive=interactive)
                self.assertTrue(c["tools"], name)
                self.assertNotIn("--tools", c["agent_args"], name)
