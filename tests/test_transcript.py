import base64
import json
import pathlib
import unittest

from cuvm import transcript

# A real smoke-run transcript reduced to tool calls and results, screenshots replaced
# by short placeholders (tests/fixtures/README.md).
SMOKE = pathlib.Path(__file__).parent / "fixtures/transcript-smoke.jsonl"


class SmokeRunTest(unittest.TestCase):
    def setUp(self):
        self.r = transcript.extract(SMOKE.read_text().splitlines(), allowed_prefixes=("mcp__computer-use__",), allowed_names=("ToolSearch",))

    def test_tools(self):
        self.assertEqual(self.r["tools_used"], ["ToolSearch", "mcp__computer-use__computer_batch",
                                                "mcp__computer-use__open_application",
                                                "mcp__computer-use__request_access",
                                                "mcp__computer-use__screenshot",
                                                "mcp__computer-use__zoom"])
        self.assertEqual(self.r["forbidden_tools_used"], [])

    def test_grants(self):
        self.assertEqual(self.r["request_access_granted"], ["TextEdit"])

    def test_input_before_final_image(self):
        self.assertTrue(self.r["input_tool_ok_before_final_shot"])

    def test_final_image(self):
        last = [json.loads(l) for l in SMOKE.read_text().splitlines()][-1]["message"]["content"][-1]["content"][-1]
        self.assertEqual(self.r["final_image_bytes"], base64.b64decode(last["source"]["data"]))
        self.assertFalse(self.r["final_image_is_error"])
        self.assertEqual(self.r["final_image_media_type"], "image/jpeg")


class SyntheticTest(unittest.TestCase):
    def test_forbidden_tool(self):
        line = '{"message":{"content":[{"type":"tool_use","id":"a","name":"Bash","input":{}}]}}'
        r = transcript.extract([line], allowed_prefixes=("mcp__computer-use__",), allowed_names=("ToolSearch",))
        self.assertEqual(r["forbidden_tools_used"], ["Bash"])

    def test_refused_forbidden_tool_is_attempted_not_used(self):
        # Observed in run smoke-20261025T194153Z-3004: the agent tried Bash and Claude Code refused it.
        use = '{"message":{"content":[{"type":"tool_use","id":"c","name":"Bash","input":{"command":"ls"}}]}}'
        res = ('{"message":{"content":[{"type":"tool_result","tool_use_id":"c","is_error":true,"content":'
               '"<tool_use_error>Error: No such tool available: Bash. Bash is disabled for this session.</tool_use_error>"}]}}')
        r = transcript.extract([use, res], allowed_prefixes=("mcp__computer-use__",), allowed_names=())
        self.assertEqual((r["forbidden_tools_attempted"], r["forbidden_tools_used"]), (["Bash"], []))

    def test_failed_command_still_counts_as_used(self):
        use = '{"message":{"content":[{"type":"tool_use","id":"d","name":"Bash","input":{"command":"false"}}]}}'
        res = '{"message":{"content":[{"type":"tool_result","tool_use_id":"d","is_error":true,"content":"Exit code 1"}]}}'
        r = transcript.extract([use, res], allowed_prefixes=("mcp__computer-use__",), allowed_names=())
        self.assertEqual(r["forbidden_tools_used"], ["Bash"])

    def test_forbidden_call_without_result_counts_as_used(self):
        use = '{"message":{"content":[{"type":"tool_use","id":"e","name":"Bash","input":{}}]}}'
        r = transcript.extract([use], allowed_prefixes=("mcp__computer-use__",), allowed_names=())
        self.assertEqual(r["forbidden_tools_used"], ["Bash"])

    def test_errored_batch_does_not_count(self):
        use = '{"message":{"content":[{"type":"tool_use","id":"b","name":"mcp__computer-use__computer_batch","input":{}}]}}'
        res = '{"message":{"content":[{"type":"tool_result","tool_use_id":"b","is_error":true,"content":[{"type":"text","text":"[1/1] type: Typed 3 grapheme(s)."}]}]}}'
        r = transcript.extract([use, res], allowed_prefixes=("mcp__computer-use__",), allowed_names=())
        self.assertFalse(r["input_tool_ok_before_final_shot"])


if __name__ == "__main__":
    unittest.main()
