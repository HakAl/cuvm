"""Browser use through a local Playwright MCP server in the guest (docs/config.md, docs/design.md).

Only active when the config sets `browser`. The server is pinned by the image record's `browser`
block: node and the MCP entry point by path, the entry point also by sha256. Its traffic goes
through the run's egress proxy like every other guest process.
"""
from cuvm import config as cfgmod

PREFIX = "mcp__playwright__"
# Install downloads browsers, upload reads guest files into a page, run_code runs code in the
# server's own Node process (shell-equivalent). None of them is needed to browse.
DENIED = ("mcp__playwright__browser_install", "mcp__playwright__browser_file_upload",
          "mcp__playwright__browser_run_code_unsafe")
CONFIG = ".cuvm/browser-mcp.json"  # under the guest home; ~/.cuvm is removed at teardown


def home(image):
    return "/Users/%s" % image["guest_user"]


def config_path(image):
    return "%s/%s" % (home(image), CONFIG)


def check_image(cfg, image):
    """The image record must have the configured engine; called once the record is loaded."""
    if "browser" not in cfg:
        return
    b = image.get("browser") or {}
    missing = [k for k in ("node", "entry", "entry_sha256", "engines") if not b.get(k)]
    if missing:
        raise cfgmod.ConfigError("browser %r needs an image record with a browser block (missing %s)"
                                 % (cfg["browser"], missing))
    if cfg["browser"] not in b["engines"]:
        raise cfgmod.ConfigError("image has no %r engine (has %s)" % (cfg["browser"], sorted(b["engines"])))


def mcp_config(cfg, image, gateway, port):
    b = image["browser"]
    args = [b["entry"], "--browser", cfg["browser"], "--proxy-server", "http://%s:%d" % (gateway, port),
            "--isolated", "--no-webmcp", "--output-dir", "%s/%s/browser" % (home(image), cfg["results"][2:])]
    if cfg.get("browser_headless"):
        args.append("--headless")
    return {"mcpServers": {"playwright": {"command": b["node"], "args": args}}}


def claude_args(cfg, image):
    """Extra claude arguments: the guest settings are dontAsk, so the server's tools must be allowed here."""
    if "browser" not in cfg:
        return []
    return ["--mcp-config", config_path(image), "--allowedTools", "mcp__playwright",
            "--disallowedTools", ",".join(DENIED)]


def tool_rules(cfg):
    """(allowed prefixes, allowed names, denied names) for transcript.extract."""
    if "browser" not in cfg:
        return ("mcp__computer-use__",), ("ToolSearch",), ()
    return ("mcp__computer-use__", PREFIX), ("ToolSearch",), DENIED
