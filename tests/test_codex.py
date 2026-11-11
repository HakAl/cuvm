"""Codex guest agent specifics (the codex design step 2): pure functions over config, image, gateway and port."""
import base64
import json
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import time
import tomllib
import unittest

from cuvm import browser, codex, config, dialog, session

FIX = pathlib.Path(__file__).parent / "fixtures" / "codex"
IMAGE = {"guest_user": "admin",
         "browser": {"node": "/opt/homebrew/bin/node", "entry": "/opt/homebrew/lib/node_modules/@playwright/mcp/cli.js",
                     "entry_sha256": "e" * 64, "engines": {"webkit": "2215"}},
         "codex": {"bin": "/opt/homebrew/bin/codex", "version": "0.167.0",
                   "binary": "/opt/homebrew/lib/node_modules/@openai/codex/bin/codex-aarch64-apple-darwin",
                   "binary_sha256": "c" * 64}}
HEADER36 = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"  # the standard RS256 JWT header, 36 characters


def cfg(**kw):
    c = {"agent": "codex", "agent_args": [], "results": "~/exp/results", "apps": [], "grants": [],
         "allow_fullscreen": False, "plan": "pro"}
    c.update(kw)
    return c


def fixture(name):
    return (FIX / name).read_text()


def b64json(seg):
    return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))


class ToolRulesTest(unittest.TestCase):
    def test_without_browser_every_mcp_name_is_forbidden(self):
        self.assertEqual(codex.tool_rules(cfg()), ((), ("exec_command", "custom:exec", "apply_patch"), ()))

    def test_with_browser(self):
        p, n, d = codex.tool_rules(cfg(browser="webkit"))
        self.assertEqual(p, (browser.PREFIX,))
        self.assertEqual(n, ("exec_command", "custom:exec", "apply_patch"))
        self.assertEqual(set(d), set(browser.DENIED))

    def test_computer_use_never_allowed(self):
        for c in (cfg(), cfg(browser="webkit")):
            p, n, d = codex.tool_rules(c)
            self.assertFalse(any("computer-use" in x for x in p + n))


class ConfigTomlTest(unittest.TestCase):
    def toml(self, c=None, mode="scripted", gateway="192.0.2.1", port=4321):
        text = codex.config_toml(c or cfg(), IMAGE, gateway, port, mode)
        return text, tomllib.loads(text)

    def test_scripted_never_interactive_on_request(self):
        _, s = self.toml(mode="scripted")
        _, i = self.toml(mode="interactive")
        self.assertEqual(s["approval_policy"], "never")
        self.assertEqual(i["approval_policy"], "on-request")
        with self.assertRaises(ValueError):
            codex.config_toml(cfg(), IMAGE, "192.0.2.1", 1, "other")

    def test_fixed_values(self):
        text, t = self.toml()
        self.assertEqual(t["sandbox_mode"], "workspace-write")
        self.assertIs(t["check_for_update_on_startup"], False)
        self.assertEqual(t["sandbox_workspace_write"], {"writable_roots": ["/Users/admin/Desktop"], "network_access": True})
        self.assertIs(t["analytics"]["enabled"], False)
        self.assertEqual(t["otel"], {"metrics_exporter": "none", "trace_exporter": "none"})
        self.assertEqual(t["history"], {"persistence": "save-all"})
        self.assertIs(t["features"]["enable_request_compression"], False)
        self.assertIs(t["tui"]["screen_reader_detection_done"], True)
        # L5 deviation 5: the ChatGPT backend's own tools (web search, image generation, Codex Apps, sub-agents) run on
        # OpenAI's side, outside net_allow; the config turns them off (0.167.0 config.schema.json: web_search is a
        # WebSearchMode, the four features are Stable keys)
        self.assertEqual(t["web_search"], "disabled")
        for k in ("apps", "plugins", "image_generation", "multi_agent", "multi_agent_v2"):
            self.assertIs(t["features"][k], False, k)
        # L7 deviation 7: multi_agent alone left the sub-agent tools on; both other switches off (L8 verifies)
        self.assertEqual(t["agents"], {"enabled": False})
        self.assertNotIn("mcp_servers", t)
        self.assertNotIn("\u2014", text)

    def test_writable_root_follows_the_image_user(self):
        _, t = self.toml()
        img = dict(IMAGE, guest_user="other")
        t2 = tomllib.loads(codex.config_toml(cfg(), img, "192.0.2.1", 1, "scripted"))
        self.assertEqual(t2["sandbox_workspace_write"]["writable_roots"], ["/Users/other/Desktop"])
        self.assertNotEqual(t, t2)

    def test_browser_block(self):
        c = cfg(browser="webkit")
        _, t = self.toml(c)
        pw = t["mcp_servers"]["playwright"]
        want = browser.mcp_config(c, IMAGE, "192.0.2.1", 4321)["mcpServers"]["playwright"]
        self.assertEqual(pw["command"], want["command"])
        self.assertEqual(pw["args"], want["args"])
        self.assertEqual(pw["args"][0], IMAGE["browser"]["entry"])
        self.assertEqual(pw["args"][pw["args"].index("--proxy-server") + 1], "http://192.0.2.1:4321")
        self.assertIn("--isolated", pw["args"])
        self.assertNotIn("--headless", pw["args"])
        self.assertNotIn("--proxy-bypass", pw["args"])
        self.assertEqual(sorted(pw["disabled_tools"]), sorted(d[len(browser.PREFIX):] for d in browser.DENIED))
        self.assertEqual(pw["default_tools_approval_mode"], "approve")

    def test_browser_headless_and_dev_server(self):
        c = cfg(browser="webkit", browser_headless=True, project="p.tgz", dev_server={"command": "npm run dev", "port": 5173, "ready_s": 60})
        _, t = self.toml(c)
        a = t["mcp_servers"]["playwright"]["args"]
        self.assertIn("--headless", a)
        self.assertEqual(a[a.index("--proxy-bypass") + 1], "localhost,127.0.0.1")

    def test_strings_round_trip(self):
        img = json.loads(json.dumps(IMAGE))
        img["browser"]["entry"] = '/odd path/with "quotes"/cli.js'
        t = tomllib.loads(codex.config_toml(cfg(browser="webkit"), img, "192.0.2.1", 1, "interactive"))
        self.assertEqual(t["mcp_servers"]["playwright"]["args"][0], img["browser"]["entry"])


class TokenTest(unittest.TestCase):
    def test_dummy_shape(self):
        run_id = "smoke-20261108T053012Z-ab12"
        tok = codex.dummy_token(run_id)
        parts = tok.split(".")
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(parts))
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9_.-]+", tok))
        self.assertEqual(b64json(parts[0]), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(parts[0], HEADER36)
        p = b64json(parts[1])
        self.assertEqual((p["iss"], p["aud"], p["sub"]), ("https://auth.openai.com", "https://api.openai.com/v1", "cuvm-dummy"))
        self.assertGreaterEqual(p["exp"] - time.time(), 700 * 86400)
        self.assertLessEqual(p["iat"], time.time())
        self.assertEqual(p["cuvm"]["run"], run_id)
        self.assertEqual(set(p) - {"iss", "aud", "sub", "iat", "exp", "cuvm"}, set())  # no account or plan claim
        self.assertNotIn("account", json.dumps(p))
        self.assertNotIn("plan", json.dumps(p))
        self.assertLess(len(tok), 400)
        self.assertNotIn("sk-ant-", tok)

    def test_two_calls_differ_and_long_ids_fit(self):
        a, b = codex.dummy_token("x"), codex.dummy_token("x")
        self.assertNotEqual(a, b)
        self.assertNotEqual(b64json(a.split(".")[1])["cuvm"]["nonce"], b64json(b.split(".")[1])["cuvm"]["nonce"])
        long_id = "n" * 50
        self.assertLess(len(codex.dummy_token(long_id)), 400)
        self.assertLess(len(codex.placeholder_id_token(long_id)), 400)
        with self.assertRaises(ValueError):
            codex.dummy_token("n" * 200)

    def test_placeholder_differs(self):
        d, i = codex.dummy_token("r"), codex.placeholder_id_token("r")
        self.assertNotEqual(d, i)
        self.assertEqual(b64json(i.split(".")[1])["sub"], "cuvm-dummy-id")
        self.assertEqual(b64json(i.split(".")[1])["cuvm"]["run"], "r")
        self.assertLess(len(i), 400)
        self.assertNotIn("sk-ant-", i)


class AuthJsonTest(unittest.TestCase):
    def test_shape(self):
        d, i = codex.dummy_token("r"), codex.placeholder_id_token("r")
        text = codex.auth_json(d, i, "acct-0000")
        a = json.loads(text)
        self.assertEqual(set(a), {"OPENAI_API_KEY", "auth_mode", "last_refresh", "tokens"})
        self.assertIsNone(a["OPENAI_API_KEY"])
        self.assertEqual(a["auth_mode"], "chatgpt")
        self.assertTrue(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", a["last_refresh"]))
        t = a["tokens"]
        self.assertEqual(set(t), {"access_token", "account_id", "id_token", "refresh_token", "login_owner_id", "oauth_provenance"})
        self.assertEqual((t["access_token"], t["id_token"], t["account_id"]), (d, i, "acct-0000"))
        self.assertEqual(t["refresh_token"], "cuvm-no-refresh-token")
        self.assertEqual(t["login_owner_id"], "cuvm-dummy")
        self.assertEqual(t["oauth_provenance"], {"issuer": "https://auth.openai.com", "client_id": "app_EMoamEEZ73f0CkXaXp7hrann"})
        self.assertTrue(text.endswith("\n"))


class CommandsTest(unittest.TestCase):
    GW, PORT = "192.0.2.1", 4321

    def test_env(self):
        e = codex.env(self.GW, self.PORT)
        self.assertTrue(e.startswith(session.env(self.GW, self.PORT)))
        self.assertIn("CODEX_CA_CERTIFICATE=$HOME/.cuvm/ca.pem", e)
        self.assertIn("HTTPS_PROXY=http://192.0.2.1:4321", e)

    def test_tui_cmd(self):
        c = codex.tui_cmd(self.GW, self.PORT, ["--model", "gpt-6 x"])
        self.assertIn("exec codex --model 'gpt-6 x'", c)
        self.assertIn("CODEX_CA_CERTIFICATE=", c)
        self.assertNotIn("security", c)
        self.assertNotIn("eyJ", c)

    def test_exec_cmd(self):
        prompt = "Write CODEX-{run_id} to ~/Desktop/codex.txt; it's \"quoted\" and costs $5"
        c = codex.exec_cmd(self.GW, self.PORT, ["--model", "m"], prompt)
        self.assertTrue(c.startswith("cd ~/exp && "))
        for s in ("</dev/null", " --json ", "--skip-git-repo-check", "CODEX_CA_CERTIFICATE=", "HTTPS_PROXY=",
                  "-o ~/.cuvm/codex-last.txt", "> ~/.cuvm/codex-events.jsonl", "2> ~/.cuvm/codex-stderr.txt",
                  "echo \"EXIT=$?\" > ~/.cuvm/codex-exit.txt", " --model m ", shlex.quote(prompt)):
            self.assertIn(s, c)
        self.assertNotIn("--ephemeral", c)
        self.assertNotIn("eyJ", c)
        # the prompt reaches codex as one argument: the shell sees the quoted form
        argv = shlex.split(c.split("&&", 1)[1].split("</dev/null")[0])
        self.assertEqual(argv[-1], prompt)

    def test_reach_cmd(self):
        c = codex.reach_cmd(self.GW, self.PORT)
        for s in ("cd ~/exp && ", "--ephemeral", " --json ", "--skip-git-repo-check", "</dev/null", "-o ~/reach.txt",
                  "'Reply with exactly PONG'", "> ~/reach-events.jsonl 2>&1", "CODEX_CA_CERTIFICATE="):
            self.assertIn(s, c)
        self.assertNotIn("eyJ", c)


class PaneTest(unittest.TestCase):
    def test_ready(self):
        self.assertTrue(codex.ready(fixture("tui-ready.txt")))
        self.assertTrue(codex.ready(fixture("tui-complete.txt")))
        self.assertFalse(codex.ready(""))
        self.assertFalse(codex.ready("  >_ OpenAI Codex (v0.167.0)\n  loading\n"))

    def test_idle(self):
        self.assertTrue(codex.idle(fixture("tui-ready.txt")))
        self.assertTrue(codex.idle(fixture("tui-complete.txt")))
        self.assertFalse(codex.idle(fixture("tui-working.txt")))
        self.assertFalse(codex.idle(fixture("tui-approval.txt")))  # the last chevron is on an option
        self.assertFalse(codex.idle(""))

    def test_idle_needs_the_status_line_near_the_last_chevron(self):
        p = fixture("tui-ready.txt").replace("  GPT-6.1-Sol default · ~/exp", "  something else")
        self.assertFalse(codex.idle(p))
        lines = fixture("tui-ready.txt").splitlines()
        i = lines.index("› Ask Codex to do anything")
        far = lines[:i + 1] + ["", "", ""] + lines[i + 1:]
        self.assertFalse(codex.idle("\n".join(far)))

    def test_parse_prompt_none_without_footer(self):
        self.assertIsNone(codex.parse_prompt(fixture("tui-ready.txt")))
        self.assertIsNone(codex.parse_prompt(fixture("tui-working.txt")))
        self.assertIsNone(codex.parse_prompt(fixture("tui-complete.txt")))

    def test_parse_prompt_command(self):
        d = codex.parse_prompt(fixture("tui-approval.txt"))
        self.assertIsInstance(d, dialog.Dialog)
        self.assertEqual(d.kind, "codex_command")
        self.assertEqual(d.extra, ["touch /Users/admin/outside-probe.txt",
                                   "May I create the requested empty file outside the workspace at /Users/admin/outside-probe.txt?"])
        self.assertEqual(d.apps, [])
        self.assertTrue(d.text.startswith("Would you like to run the following command?"))
        self.assertIn("Press enter to confirm or esc to cancel", d.text)
        self.assertEqual(d.kind in codex.PROMPT_KINDS, True)

    def test_wrapped_option_and_reason_lines_are_rejoined(self):
        # L5 run codex-20261108T190956Z-7c4e dialog 1 (the retained live-run evidence L5, deviation 4): option 2 wrapped at the
        # terminal width and the Reason ran over two lines; the pane as recorded plus the footer line
        d = codex.parse_prompt(fixture("tui-approval-wrapped.txt"))
        self.assertEqual(d.kind, "codex_command")
        self.assertEqual(d.extra, ["printf 'L5\\n' > /Users/admin/Documents/codex-l5.txt",
                                   "May I create ~/Documents/codex-l5.txt with the line L5? This location is outside the writable sandbox roots."])
        self.assertEqual(d.text.count("\n") + 1, 10)  # START to FOOTER, the wrapped line kept as two

    def test_wrapped_command_line_is_rejoined(self):
        base = fixture("tui-approval.txt")
        p = base.replace("  $ touch /Users/admin/outside-probe.txt\n", "  $ touch /Users/admin/outside-pr\n  obe.txt\n")
        d = codex.parse_prompt(p)
        self.assertEqual((d.kind, d.extra[0]), ("codex_command", "touch /Users/admin/outside-probe.txt"))

    def test_patch_prompt(self):
        # L5 dialog 2: the apply_patch approval for a file outside the writable roots
        d = codex.parse_prompt(fixture("tui-patch-approval.txt"))
        self.assertEqual(d.kind, "codex_patch")
        self.assertIn(d.kind, codex.PROMPT_KINDS)
        self.assertEqual(d.extra, ["/Users/admin/Documents/codex-l5.txt", "Apply proposed file edits"])
        self.assertEqual(d.apps, [])
        self.assertTrue(d.text.startswith("Would you like to make the following edits?"))
        self.assertIn("Press enter to confirm or esc to cancel", d.text)
        self.assertFalse(codex.idle(fixture("tui-patch-approval.txt")))

    def test_patch_prompt_without_destination_or_with_command_options_is_unknown(self):
        base = fixture("tui-patch-approval.txt")
        self.assertEqual(codex.parse_prompt(base.replace("  Destination: /Users/admin/Documents/codex-l5.txt\n", "")).kind, "unknown")
        swapped = base.replace("  2. Yes, and don't ask again for these files (a)", "  2. Yes, and don't ask again for commands that start with `x` (p)")
        self.assertEqual(codex.parse_prompt(swapped).kind, "unknown")

    def test_header_text_inside_the_reason_does_not_start_the_block(self):
        # agy CL5-3: the headers are matched as whole lines, not substrings
        base = fixture("tui-approval.txt")
        quoted = base.replace("Reason: May I create", "Reason: Would you like to run the following command? May I create")
        d = codex.parse_prompt(quoted)
        self.assertEqual(d.kind, "codex_command")
        self.assertEqual(d.extra[0], "touch /Users/admin/outside-probe.txt")
        self.assertTrue(d.extra[1].startswith("Would you like to run the following command? May I create"))
        self.assertEqual(codex.parse_prompt(base.replace("  Would you like to run", "  • Would you like to run")).kind, "unknown")

    def test_cursor_on_any_option(self):
        base = fixture("tui-approval.txt")
        want = codex.parse_prompt(base)
        moved2 = base.replace("› 1. Yes, proceed (y)", "  1. Yes, proceed (y)").replace("  2. Yes, and", "› 2. Yes, and")
        moved3 = base.replace("› 1. Yes, proceed (y)", "  1. Yes, proceed (y)").replace("  3. No, and", "› 3. No, and")
        for p in (moved2, moved3):
            d = codex.parse_prompt(p)
            self.assertEqual((d.kind, d.extra), (want.kind, want.extra))
        two = base.replace("  2. Yes, and", "› 2. Yes, and")  # two cursors is not a shape we have seen
        self.assertEqual(codex.parse_prompt(two).kind, "unknown")

    def test_missing_option_is_unknown(self):
        lines = [l for l in fixture("tui-approval.txt").splitlines() if not l.startswith("  3. No, and")]
        d = codex.parse_prompt("\n".join(lines))
        self.assertEqual(d.kind, "unknown")
        self.assertLessEqual(len(d.text.splitlines()), 15)
        self.assertIn("Would you like to run the following command?", d.text)
        self.assertEqual(d.extra, [])

    def test_footer_only_is_unknown(self):
        pane = "\n".join(["line %d" % i for i in range(30)] + ["  Press enter to confirm or esc to cancel"])
        d = codex.parse_prompt(pane)
        self.assertEqual(d.kind, "unknown")
        self.assertEqual(len(d.text.splitlines()), 15)
        self.assertTrue(d.text.startswith("line 15"))

    def test_missing_command_line_is_unknown(self):
        p = fixture("tui-approval.txt").replace("  $ touch /Users/admin/outside-probe.txt\n", "")
        self.assertEqual(codex.parse_prompt(p).kind, "unknown")


class TranscriptsTest(unittest.TestCase):
    def run_listing(self, home):
        return subprocess.run(["zsh", "-c", codex.TRANSCRIPTS], env=dict(os.environ, HOME=str(home)),
                              capture_output=True, text=True, timeout=30)

    def test_text(self):
        self.assertIn('find -L "$d" -mindepth 4 -maxdepth 4 -name "rollout-*.jsonl"', codex.TRANSCRIPTS)
        self.assertIn('"$h/.codex"', codex.TRANSCRIPTS)
        self.assertIn('"$c/sessions"', codex.TRANSCRIPTS)

    def test_absent_sessions_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_listing(tmp)
            self.assertEqual((r.returncode, r.stdout), (0, ""))
            (pathlib.Path(tmp) / ".codex").mkdir()
            r = self.run_listing(tmp)
            self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_lists_rollouts_at_depth_four(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp) / ".codex/sessions/2026/11/08"
            d.mkdir(parents=True)
            (d / "rollout-2026-11-08T09-25-48-abc.jsonl").write_text("{}\n")
            (d / "other.jsonl").write_text("{}\n")
            (pathlib.Path(tmp) / ".codex/sessions/rollout-top.jsonl").write_text("{}\n")
            r = self.run_listing(tmp)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.split(), [str(d / "rollout-2026-11-08T09-25-48-abc.jsonl")])

    def test_wrong_type_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / ".codex").mkdir()
            (pathlib.Path(tmp) / ".codex/sessions").write_text("not a dir")
            r = self.run_listing(tmp)
            self.assertEqual(r.returncode, 2)
            self.assertIn("transcripts:", r.stderr)


def ev(type_, **kw):
    return json.dumps(dict(type=type_, **kw))


def item(type_, i="item_0", **fields):
    return ev("item.completed", item=dict(id=i, type=type_, **fields))


def mcp(tool, status="completed", text=None, error=None, server="playwright", arguments=None, i="item_x"):
    result = None if text is None else {"content": [{"type": "text", "text": text}], "structured_content": None}
    err = None if error is None else {"message": error}
    return item("mcp_tool_call", i, server=server, tool=tool, arguments=arguments or {}, result=result, error=err, status=status)


USAGE1 = {"input_tokens": 100, "cached_input_tokens": 50, "cache_write_input_tokens": 0, "output_tokens": 6, "reasoning_output_tokens": 0}
USAGE2 = {"input_tokens": 200, "cached_input_tokens": 150, "cache_write_input_tokens": 0, "output_tokens": 20, "reasoning_output_tokens": 4}


def stream(install_status="failed", extra=()):
    return [
        ev("thread.started", thread_id="synthetic-thread-1"),
        ev("turn.started"),
        item("agent_message", "item_0", text="I will open the page."),
        ev("item.started", item={"id": "item_1", "type": "mcp_tool_call", "server": "playwright", "tool": "browser_navigate",
                                  "arguments": {"url": "https://registry.npmjs.org/@openai/codex/latest"}, "result": None,
                                  "error": None, "status": "in_progress"}),
        mcp("browser_navigate", text="### Page\n- Page URL: https://registry.npmjs.org/@openai/codex/latest",
            arguments={"url": "https://registry.npmjs.org/@openai/codex/latest"}, i="item_1"),
        mcp("browser_navigate", status="failed", error="MCP tool call requires approval, but approval policy is never",
            arguments={"url": "https://refused.example/"}, i="item_2"),
        item("command_execution", "item_3", command="ls ~/exp", aggregated_output="a\n", exit_code=0, status="completed"),
        mcp("browser_console_messages", text="- [ERROR] boom\n- [LOG] fine\n- [error] again", i="item_4"),
        mcp("browser_network_requests", text="[GET] http://x => [404] Not Found\n[GET] http://y => [200] OK\n[GET] http://z => [FAILED] net::ERR_X", i="item_5"),
        mcp("browser_install", status=install_status, error=None if install_status == "completed" else "unavailable", i="item_6"),
        mcp("x", server="other", i="item_7"),
        mcp("left_click", server="computer-use", i="item_8"),
        item("reasoning", "item_9", text="thinking"),
        ev("turn.completed", usage=USAGE1),
        *extra,
        ev("turn.started"),
        item("agent_message", "item_10", text="VERSION: 0.167.0"),
        ev("turn.completed", usage=USAGE2),
    ]


class ExtractTest(unittest.TestCase):
    def test_tools_used_and_forbidden_with_browser(self):
        r = codex.extract(stream(), *codex.tool_rules(cfg(browser="webkit")))
        self.assertEqual(r["tools_used"], ["exec_command", "mcp__computer-use__left_click", "mcp__other__x",
                                           "mcp__playwright__browser_console_messages", "mcp__playwright__browser_install",
                                           "mcp__playwright__browser_navigate", "mcp__playwright__browser_network_requests"])
        self.assertEqual(r["forbidden_tools_attempted"], ["mcp__computer-use__left_click", "mcp__other__x", "mcp__playwright__browser_install"])
        self.assertEqual(r["forbidden_tools_used"], ["mcp__computer-use__left_click", "mcp__other__x"])
        self.assertEqual(r["items"]["agent_message"], 2)
        self.assertEqual(r["items"]["reasoning"], 1)
        self.assertEqual(r["items"]["mcp_tool_call"], 7)  # item.completed only; the item.started is not an item
        self.assertEqual(r["items"]["command_execution"], 1)

    def test_file_change_is_apply_patch_and_unknown_item_types_are_forbidden(self):
        # L5 deviation 5: a completed item of a type the judge does not know is a tool it cannot vouch for
        lines = [ev("thread.started", thread_id="t1"), item("reasoning", "r1", text="hm"),
                 item("file_change", "f1", status="completed", changes=[{"path": "/Users/admin/exp/a.txt", "kind": "update"}]),
                 item("web_search", "w1", query="x", action={"type": "search"}),  # no status, exec_events.rs 298
                 item("todo_list", "t1", items=[{"text": "plan", "completed": False}]), item("error", "e1", message="boom"),
                 item("collab_tool_call", "c1", tool="spawn_agent", status="failed"),
                 item("agent_message", "m1", text="DONE"), ev("turn.completed", usage={"input_tokens": 1})]
        r = codex.extract(lines, *codex.tool_rules(cfg()))
        self.assertEqual(r["tools_used"], ["apply_patch", "item:collab_tool_call", "item:web_search"])
        self.assertEqual((r["forbidden_tools_attempted"], r["forbidden_tools_used"]),
                         (["item:collab_tool_call", "item:web_search"], ["item:web_search"]))
        self.assertEqual(r["items"], {"reasoning": 1, "file_change": 1, "web_search": 1, "todo_list": 1, "error": 1,
                                      "collab_tool_call": 1, "agent_message": 1})
        self.assertEqual(r["error_events"], ["boom"])

    def test_completed_denied_tool_is_used(self):
        r = codex.extract(stream(install_status="completed"), *codex.tool_rules(cfg(browser="webkit")))
        self.assertIn("mcp__playwright__browser_install", r["forbidden_tools_used"])

    def test_without_browser_every_mcp_name_is_forbidden(self):
        r = codex.extract(stream(), *codex.tool_rules(cfg()))
        self.assertNotIn("exec_command", r["forbidden_tools_attempted"])
        self.assertEqual(r["forbidden_tools_attempted"], [t for t in r["tools_used"] if t.startswith("mcp__")])
        self.assertIn("mcp__computer-use__left_click", r["forbidden_tools_used"])
        self.assertIn("mcp__playwright__browser_navigate", r["forbidden_tools_used"])

    def test_browser_rules_from_browser_module_would_be_wrong(self):
        r = codex.extract(stream(), *browser.tool_rules(cfg(browser="webkit")))
        self.assertIn("exec_command", r["forbidden_tools_attempted"])  # why codex.tool_rules exists (CP2-1)

    def test_browser_fields(self):
        r = codex.extract(stream(), *codex.tool_rules(cfg(browser="webkit")))
        self.assertEqual(r["browser_navigated_hosts"], ["registry.npmjs.org"])
        self.assertEqual(r["browser_hosts_before_input"], ["registry.npmjs.org"])
        self.assertEqual(r["browser_console_errors"], 2)
        self.assertEqual(r["browser_network_failed"], 2)

    def test_navigate_with_non_dict_arguments_records_no_host(self):
        # CW-1 (agy work review): a string or list in `arguments` must not raise; only a dict carries a url
        extra = [mcp("browser_navigate", text="ok", arguments='{"url": "https://string.example/"}', i="item_s"),
                 mcp("browser_navigate", text="ok", arguments=["https://list.example/"], i="item_l")]
        r = codex.extract(stream(extra=extra), *codex.tool_rules(cfg(browser="webkit")))
        self.assertEqual(r["browser_navigated_hosts"], ["registry.npmjs.org"])
        self.assertEqual(r["items"]["mcp_tool_call"], 9)

    def test_computer_use_fields_are_null_or_empty(self):
        r = codex.extract(stream(), *codex.tool_rules(cfg(browser="webkit")))
        self.assertEqual(r["request_access_granted"], [])
        self.assertIsNone(r["input_tool_ok_before_final_shot"])
        for k in ("final_image_sha256", "final_image_is_error", "final_image_media_type", "final_image_bytes"):
            self.assertIsNone(r[k])

    def test_usage_thread_and_message(self):
        r = codex.extract(stream(), *codex.tool_rules(cfg(browser="webkit")))
        self.assertEqual(r["codex_thread_id"], "synthetic-thread-1")
        self.assertEqual(r["codex_usage"]["last"], USAGE2)
        self.assertEqual(r["codex_usage"]["turns"], 2)
        self.assertEqual(r["codex_usage"]["sum"], {k: USAGE1[k] + USAGE2[k] for k in USAGE1})
        self.assertEqual(r["codex_last_message"], "VERSION: 0.167.0")
        self.assertEqual(r["error_events"], [])
        self.assertEqual(r["unparsed_lines"], 0)

    def test_last_message_is_bounded(self):
        r = codex.extract([item("agent_message", text="x" * 5000)], *codex.tool_rules(cfg()))
        self.assertEqual(len(r["codex_last_message"]), 2000)

    def test_error_events_and_garbage(self):
        extra = (ev("turn.failed", error={"message": "stream disconnected"}), "not json {", "", ev("error", message="boom"))
        r = codex.extract(stream(extra=extra), *codex.tool_rules(cfg(browser="webkit")))
        self.assertEqual(r["error_events"], ["stream disconnected", "boom"])
        self.assertNotIn("turn_failed", r)
        self.assertEqual(r["unparsed_lines"], 1)

    def test_empty_stream(self):
        r = codex.extract((), *codex.tool_rules(cfg()))
        self.assertEqual((r["tools_used"], r["codex_thread_id"], r["codex_last_message"]), ([], None, None))
        self.assertEqual(r["codex_usage"], {"last": None, "sum": {}, "turns": 0})
        self.assertEqual(r["items"], {})


def rec(type_, payload, ts="2026-11-08T09:19:54.200Z"):
    return json.dumps({"timestamp": ts, "type": type_, "payload": payload})


TOKENS1 = {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 1}}}
TOKENS2 = {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 30, "output_tokens": 5}}}


def rollout(mcp_item=None):
    lines = [
        rec("session_meta", {"id": "01a1", "cwd": "/Users/admin/exp", "originator": "codex-tui", "cli_version": "0.167.0"}),
        rec("event_msg", {"type": "task_started", "turn_id": "t1"}),
        rec("response_item", {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "You are Codex"}]}),
        rec("response_item", {"type": "custom_tool_call", "status": "completed", "call_id": "c1", "name": "exec",
                              "input": "text(await tools.exec_command({cmd:\"touch /Users/admin/outside-probe.txt\"}))"}),
        rec("event_msg", {"type": "item_completed", "thread_id": "01a1", "turn_id": "t1",
                          "item": {"type": "CommandExecution", "id": "exec-1", "command": ["/bin/zsh", "-lc", "touch /Users/admin/outside-probe.txt"],
                                   "cwd": "file:///Users/admin/exp", "status": "completed"}}),
        rec("event_msg", TOKENS1),
        rec("response_item", {"type": "custom_tool_call_output", "call_id": "c1", "output": "ok"}),
        rec("event_msg", {"type": "item_completed", "thread_id": "01a1", "turn_id": "t1",
                          "item": {"type": "AgentMessage", "id": "m1", "text": "DONE"}}),
        rec("event_msg", TOKENS2),
        rec("event_msg", {"type": "task_complete", "turn_id": "t1"}),
    ]
    if mcp_item:
        lines.insert(5, rec("event_msg", {"type": "item_completed", "thread_id": "01a1", "turn_id": "t1", "item": mcp_item}))
    return lines


class ExtractRolloutTest(unittest.TestCase):
    def test_captured_reasoning_is_not_a_tool_and_unknown_still_fails(self):
        lines = fixture("rollout-reasoning.jsonl").splitlines()
        r = codex.extract_rollout(lines, *codex.tool_rules(cfg()))
        self.assertEqual(r["tools_used"], [])
        self.assertEqual(r["forbidden_tools_used"], [])
        unknown = [line.replace('"Reasoning"', '"FutureUnknown"') for line in lines]
        self.assertEqual(codex.extract_rollout(unknown, *codex.tool_rules(cfg()))["forbidden_tools_used"],
                         ["item:FutureUnknown"])

    def test_codex_tools_are_ordinary(self):
        r = codex.extract_rollout(rollout(), *codex.tool_rules(cfg()))
        self.assertEqual(r["tools_used"], ["custom:exec", "exec_command"])
        self.assertEqual((r["forbidden_tools_attempted"], r["forbidden_tools_used"]), ([], []))

    def test_usage_is_the_last_token_count_not_a_sum(self):
        r = codex.extract_rollout(rollout(), *codex.tool_rules(cfg()))
        self.assertEqual(r["codex_usage"], {"info": {"total_token_usage": {"input_tokens": 30, "output_tokens": 5}}})

    def test_merge_loop_keys(self):
        r = codex.extract_rollout(rollout(), *codex.tool_rules(cfg()))
        for k in ("tools_used", "forbidden_tools_attempted", "forbidden_tools_used", "request_access_granted", "browser_navigated_hosts"):
            self.assertIsInstance(r[k], list)
        self.assertEqual((r["request_access_granted"], r["browser_navigated_hosts"]), ([], []))
        self.assertEqual((r["browser_console_errors"], r["browser_network_failed"]), (0, 0))
        self.assertEqual(r["unparsed_lines"], 0)

    def test_mcp_item_best_effort(self):
        it = {"type": "McpToolCall", "id": "mcp-1", "server": "playwright", "tool": "browser_navigate", "status": "completed"}
        r = codex.extract_rollout(rollout(it), *codex.tool_rules(cfg(browser="webkit")))
        self.assertIn("mcp__playwright__browser_navigate", r["tools_used"])
        self.assertEqual(r["forbidden_tools_attempted"], [])
        r = codex.extract_rollout(rollout(it), *codex.tool_rules(cfg()))
        self.assertEqual(r["forbidden_tools_used"], ["mcp__playwright__browser_navigate"])
        it2 = dict(it, server="other", tool="y", status="failed")
        r = codex.extract_rollout(rollout(it2), *codex.tool_rules(cfg(browser="webkit")))
        self.assertEqual((r["forbidden_tools_attempted"], r["forbidden_tools_used"]), (["mcp__other__y"], []))

    def test_mcp_items_from_the_observed_rollout(self):
        # the item lines of the L3 web run codex-20261108T145212Z-7aac (the retained live-run evidence L3), results and ids cut:
        # McpToolCall carries server, tool, arguments, readOnlyHint, status and result; shell calls are CommandExecution
        # items; every call goes through the custom `exec` tool
        lines = fixture("rollout-web-items.jsonl").splitlines()
        calls = [json.loads(line)["payload"] for line in lines
                 if json.loads(line)["payload"].get("type") == "custom_tool_call"]
        self.assertEqual([sorted(call) for call in calls],
                         [["call_id", "id", "input", "internal_chat_message_metadata_passthrough",
                           "name", "status", "type"]] * 4)
        r = codex.extract_rollout(lines, *codex.tool_rules(cfg(browser="webkit")))
        self.assertEqual(r["tools_used"], ["custom:exec", "exec_command", "mcp__playwright__browser_navigate",
                                           "mcp__playwright__browser_snapshot"])
        self.assertEqual((r["forbidden_tools_attempted"], r["forbidden_tools_used"], r["unparsed_lines"]), ([], [], 0))
        r = codex.extract_rollout(lines, *codex.tool_rules(cfg()))
        self.assertEqual(r["forbidden_tools_used"], ["mcp__playwright__browser_navigate", "mcp__playwright__browser_snapshot"])
        items = [json.loads(line)["payload"]["item"] for line in lines if '"McpToolCall"' in line]
        self.assertEqual([sorted(it) for it in items],
                         [["arguments", "duration", "id", "readOnlyHint", "result", "server", "status", "tool", "type"]] * 2)
        self.assertEqual([(it["server"], it["tool"], it["status"]) for it in items],
                         [("playwright", "browser_navigate", "completed"), ("playwright", "browser_snapshot", "completed")])
        self.assertNotIn("01a1bba6", "".join(lines))  # ids cut: no raw thread or turn id left

    def test_l5_items_name_apply_patch_and_the_server_side_web_search(self):
        # the item lines of the L5 interactive run codex-20261108T190956Z-7c4e (the retained live-run evidence L5), results and
        # ids cut: FileChange is Codex's apply_patch (ordinary), Extension kind web.search is the backend's web tool,
        # which never touched the guest network and is forbidden (deviation 5)
        lines = fixture("rollout-l5-items.jsonl").splitlines()
        calls = [json.loads(line)["payload"] for line in lines
                 if json.loads(line)["payload"].get("type") == "custom_tool_call"]
        self.assertEqual([sorted(call) for call in calls],
                         [["call_id", "id", "input", "internal_chat_message_metadata_passthrough",
                           "name", "status", "type"]] * 6)
        r = codex.extract_rollout(lines, *codex.tool_rules(cfg(browser="webkit")))
        self.assertEqual(r["tools_used"], ["apply_patch", "custom:exec", "exec_command", "extension:web.search",
                                           "mcp__playwright__browser_navigate", "mcp__playwright__browser_snapshot"])
        self.assertEqual((r["forbidden_tools_attempted"], r["forbidden_tools_used"]), (["extension:web.search"], ["extension:web.search"]))
        self.assertEqual(r["unparsed_lines"], 0)
        u = r["codex_usage"]
        self.assertEqual(sorted(u), ["info", "rate_limits"])
        self.assertEqual(sorted(u["info"]), ["last_token_usage", "last_usage_includes_reasoning", "model_context_window", "total_token_usage"])
        self.assertEqual(sorted(u["info"]["total_token_usage"]), ["cache_write_input_tokens", "cached_input_tokens", "input_tokens",
                                                                   "output_tokens", "reasoning_output_tokens", "total_tokens"])
        self.assertEqual((u["info"]["total_token_usage"]["total_tokens"], u["info"]["model_context_window"]), (151958, 475000))
        self.assertEqual(sorted(u["rate_limits"]["primary"]), ["resets_at", "used_percent", "window_minutes"])
        ext = [json.loads(l)["payload"]["item"] for l in lines if '"Extension"' in l]
        self.assertEqual([sorted(it) for it in ext], [["action", "id", "kind", "query", "results", "type"]])
        self.assertEqual(ext[0]["kind"], "web.search")
        fc = [json.loads(l)["payload"]["item"] for l in lines if '"FileChange"' in l]
        self.assertEqual([sorted(it) for it in fc], [["changes", "id", "status", "stderr", "stdout", "type"]] * 2)
        self.assertNotIn("01a1bc9", "".join(lines))

    def test_unknown_item_type_is_forbidden_messages_are_not(self):
        it = {"type": "SomethingNew", "id": "x1", "status": "completed"}
        r = codex.extract_rollout(rollout(it), *codex.tool_rules(cfg(browser="webkit")))
        self.assertEqual((r["forbidden_tools_attempted"], r["forbidden_tools_used"]), (["item:SomethingNew"], ["item:SomethingNew"]))
        um = {"type": "UserMessage", "id": "u1", "content": [{"type": "text", "text": "hi"}]}
        r = codex.extract_rollout(rollout(um), *codex.tool_rules(cfg()))
        self.assertEqual(r["tools_used"], ["custom:exec", "exec_command"])

    def test_garbage_and_empty(self):
        r = codex.extract_rollout(["{", "", rec("event_msg", {"type": "task_complete"})], *codex.tool_rules(cfg()))
        self.assertEqual((r["unparsed_lines"], r["tools_used"], r["codex_usage"]), (1, [], None))


class SumUsageTest(unittest.TestCase):
    def test_sums_numbers_recursively(self):
        a = {"info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 1}}, "model": "x"}
        b = {"info": {"total_token_usage": {"input_tokens": 30, "output_tokens": 5}, "extra": 2}}
        self.assertEqual(codex.sum_usage([a, b, None]), {"info": {"total_token_usage": {"input_tokens": 40, "output_tokens": 6}, "extra": 2}})
        self.assertEqual(codex.sum_usage([]), {})

    def test_only_total_token_usage_is_summed(self):
        # the L5 payload shape (the retained live-run evidence L5): a context window size and rate limits are not usage
        a = {"info": {"total_token_usage": {"input_tokens": 10, "total_tokens": 11}, "last_token_usage": {"input_tokens": 10},
                      "model_context_window": 475000}, "rate_limits": {"primary": {"used_percent": 40.0, "window_minutes": 10080}}}
        b = {"info": {"total_token_usage": {"input_tokens": 30, "total_tokens": 33}, "last_token_usage": {"input_tokens": 5},
                      "model_context_window": 475000}, "rate_limits": {"primary": {"used_percent": 100.0, "window_minutes": 10080}}}
        s = codex.sum_usage([a, b])
        self.assertEqual(s["info"]["total_token_usage"], {"input_tokens": 40, "total_tokens": 44})
        self.assertEqual((s["info"]["last_token_usage"], s["info"]["model_context_window"]), ({"input_tokens": 5}, 475000))
        self.assertEqual(s["rate_limits"]["primary"]["used_percent"], 100.0)
        self.assertEqual(codex.sum_usage([{"x": 1}]), {"x": 1})

    def test_null_info_in_the_last_payload_keeps_the_totals_and_its_rate_limits(self):
        # Lookout R6-F1: TokenCountEvent.info is Option (protocol.rs 2584); a rollout whose last token_count has no
        # usage yet must not abort collection (observed before the fix: TypeError, runs/codex-fix-r12-before.out)
        a = {"info": {"total_token_usage": {"input_tokens": 10, "total_tokens": 11}, "model_context_window": 475000},
             "rate_limits": {"primary": {"used_percent": 40.0}}}
        b = {"info": None, "rate_limits": {"primary": {"used_percent": 41.0}}}
        s = codex.sum_usage([a, b])
        self.assertEqual(s, {"info": {"total_token_usage": {"input_tokens": 10, "total_tokens": 11}},
                             "rate_limits": {"primary": {"used_percent": 41.0}}})
        self.assertEqual(codex.sum_usage([b]), b)
        self.assertEqual(codex.sum_usage([{"info": None, "rate_limits": None}, a]), a)
        # agy CL5R3-1: a non-dict info (never valid upstream) is skipped, not a crash
        self.assertEqual(codex.sum_usage([a, {"info": "x"}]), {"info": {"total_token_usage": {"input_tokens": 10, "total_tokens": 11}}})
        self.assertEqual(codex.sum_usage([{"info": [1]}]), {"info": [1]})


class CheckImageTest(unittest.TestCase):
    def test_claude_needs_nothing(self):
        codex.check_image(cfg(agent="claude"), {"guest_user": "admin"})

    def test_accepts_the_block(self):
        codex.check_image(cfg(), IMAGE)

    def test_refuses_missing_block_key_or_other_version(self):
        with self.assertRaises(config.ConfigError):
            codex.check_image(cfg(), {"guest_user": "admin"})
        img = json.loads(json.dumps(IMAGE)); del img["codex"]["binary_sha256"]
        with self.assertRaises(config.ConfigError):
            codex.check_image(cfg(), img)
        img = json.loads(json.dumps(IMAGE)); img["codex"]["version"] = "0.168.0"
        with self.assertRaises(config.ConfigError) as cm:
            codex.check_image(cfg(), img)
        self.assertIn("0.167.0", str(cm.exception))


class E2Test(unittest.TestCase):
    REGEX = r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{200}[A-Za-z0-9_-]{200,}"  # 400 or more; BSD grep caps a count at 255

    def test_text(self):
        c = codex.e2_cmd()
        for s in (self.REGEX, "/usr/bin/grep -a", "--exclude-dir=node_modules", "~/.codex(N)", "~/exp(N)", "~/.zsh*(N)", "/etc/zshenv(N)",
                  "HIT:", "</dev/null", "auth.json", "OPENAI_API_KEY", "CODEX_API_KEY", "E2.1", "E2.2", "E2.3",
                  "st=$?", "scan incomplete", "'%s' /dev/null ~/.codex(N)" % self.REGEX):
            self.assertIn(s, c)
        self.assertNotIn("eyJ" + "h", c)  # no token in the script, only the pattern

    def test_regex_tells_real_from_dummy(self):
        rx = re.compile(self.REGEX)
        real = HEADER36 + "." + "A" * 1000 + "." + "B" * 300
        self.assertTrue(rx.search(real))
        self.assertIsNone(rx.search(codex.dummy_token("smoke-20261108T053012Z-ab12")))
        self.assertIsNone(rx.search(codex.placeholder_id_token("smoke-20261108T053012Z-ab12")))
        text = "x " + codex.dummy_token("r") + " and " + real
        self.assertEqual(len(rx.findall(text)), 1)

    def test_script_runs_against_a_fake_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / "exp/node_modules").mkdir(parents=True)
            (home / ".codex").mkdir()
            env = dict(os.environ, HOME=str(home))
            env.pop("OPENAI_API_KEY", None); env.pop("CODEX_API_KEY", None)
            r = subprocess.run(["zsh", "-c", codex.e2_cmd()], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotIn("HIT:", r.stdout)
            real = HEADER36 + "." + "A" * 1000 + "." + "B" * 300
            (home / "exp/node_modules/ignored.txt").write_text(real)
            (home / ".codex/dummy.txt").write_text(codex.dummy_token("r"))
            r = subprocess.run(["zsh", "-c", codex.e2_cmd()], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            (home / ".codex/leak.txt").write_text("token=" + real)
            r = subprocess.run(["zsh", "-c", codex.e2_cmd()], env=env, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("HIT: %s" % (home / ".codex/leak.txt"), r.stdout)
            self.assertNotIn(real, r.stdout + r.stderr)
            (home / ".codex/leak.txt").unlink()
            (home / ".codex/auth.json").write_text("{}")
            r = subprocess.run(["zsh", "-c", codex.e2_cmd()], env=env, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("E2.1 FAIL", r.stdout)
            (home / ".codex/auth.json").unlink()
            r = subprocess.run(["zsh", "-c", codex.e2_cmd()], env=dict(env, OPENAI_API_KEY="k"), capture_output=True, text=True, timeout=30)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("E2.3 FAIL", r.stdout)
            self.assertNotIn("k", r.stdout.split("E2.3")[1].split("\n")[0].replace("E2.3 FAIL", "").replace("key", ""))

    @unittest.skipIf(os.path.exists("/etc/zshenv"), "a host /etc/zshenv gives grep an operand; the case is not reproducible")
    def test_all_scan_paths_absent_scans_nothing_not_the_working_directory(self):
        # agy CW2-1: BSD grep -r with zero path operands walks `.` instead of reading </dev/null (observed on the host)
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "home"
            home.mkdir()  # no ~/exp, no ~/.codex, no ~/.zsh*: every (N) glob is empty
            cwd = pathlib.Path(tmp) / "cwd/sub"
            cwd.mkdir(parents=True)
            (cwd / "leak.txt").write_text(HEADER36 + "." + "A" * 1000 + "." + "B" * 300)
            env = dict(os.environ, HOME=str(home))
            env.pop("OPENAI_API_KEY", None); env.pop("CODEX_API_KEY", None)
            r = subprocess.run(["zsh", "-c", codex.e2_cmd()], env=env, cwd=cwd.parent, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("E2.2 ok", r.stdout)
            self.assertNotIn("HIT:", r.stdout)

    @unittest.skipIf(os.geteuid() == 0, "root reads every file")
    def test_unreadable_file_is_an_incomplete_scan_not_a_clean_one(self):
        # Lookout COMPUTERUSE-CODEX-001 R1-F1: grep exit 2 (an I/O error) must fail E2.2, not pass as "no hit"
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / "exp").mkdir()
            secret = home / "exp/secret.txt"
            secret.write_text("nothing readable here")
            secret.chmod(0)
            env = dict(os.environ, HOME=str(home))
            env.pop("OPENAI_API_KEY", None); env.pop("CODEX_API_KEY", None)
            try:
                r = subprocess.run(["zsh", "-c", codex.e2_cmd()], env=env, capture_output=True, text=True, timeout=30)
            finally:
                secret.chmod(0o600)
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("E2.2 FAIL scan incomplete (grep exit 2)", r.stdout)
            self.assertNotIn("HIT:", r.stdout)
            self.assertNotIn("E2.2 ok", r.stdout)
            self.assertNotIn("secret", r.stdout)  # the unreadable path is not named either

    def test_e3_and_remove(self):
        e3 = codex.e3_cmd()
        self.assertIn("~/.codex/auth.json", e3)
        self.assertIn("sha256", e3)
        self.assertIn("access_token", e3)
        self.assertNotIn("eyJ", e3)
        rm = codex.remove_cmd()
        self.assertTrue(rm.startswith("rm -f ~/.codex/auth.json"))
        self.assertIn(codex.e2_cmd(), rm)

    def test_e3_runs_against_a_fake_home(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / ".codex").mkdir()
            d = codex.dummy_token("r")
            (home / ".codex/auth.json").write_text(codex.auth_json(d, codex.placeholder_id_token("r"), "a"))
            r = subprocess.run(["zsh", "-c", codex.e3_cmd()], env=dict(os.environ, HOME=str(home)), capture_output=True, text=True, timeout=30)
            self.assertEqual(r.stdout.strip(), hashlib.sha256(d.encode()).hexdigest())
            self.assertNotIn(d, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
