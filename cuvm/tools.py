"""Guest tools (the guest-tools design): a config may list built-in Claude Code tools the agent may use in
the guest. The image's settings deny them all; a deny rule wins over every allow and flag, so the run
installs a per-run settings file in the clone that allows exactly the listed ones (run.install_tools),
passes them to `claude --tools`, and tells the transcript judge they are ordinary (browser.tool_rules)."""
import json

from cuvm import DATA

TOOLS = ("Bash", "Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep")
BASE = DATA / "guest-settings.json"  # what the image installs; images/<name>.json pins its sha256


def settings(tools, base=BASE):
    """The per-run settings text: `base` with the listed tools moved from `permissions.deny` to
    `permissions.allow` (appended in TOOLS order), everything else unchanged."""
    tools = list(tools)
    if not tools or set(tools) - set(TOOLS):
        raise ValueError("tools must be a non-empty subset of %s: %r" % (TOOLS, tools))
    s = json.loads(base.read_text())
    s["permissions"]["deny"] = [d for d in s["permissions"]["deny"] if d not in tools]
    s["permissions"]["allow"] = s["permissions"]["allow"] + [t for t in TOOLS if t in tools]
    return json.dumps(s, indent=2) + "\n"


def claude_args(cfg):
    """`--tools` names the built-in set for the session: ToolSearch (the MCP tools load through it) plus
    the configured tools. An unlisted built-in tool does not exist in the session whatever the settings
    say, and the per-run settings still deny the rest; no second --disallowedTools is added."""
    if not cfg.get("tools"):
        return []
    return ["--tools", ",".join(["ToolSearch", *cfg["tools"]])]


def allowed_names(cfg):
    """Built-in tool names the transcript judge treats as ordinary."""
    return ("ToolSearch", *cfg.get("tools", ()))
