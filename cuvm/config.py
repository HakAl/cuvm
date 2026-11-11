"""Experiment config: load, validate strictly, hash.

Relative paths (`inputs`, `net_allow`) resolve against the config file's directory.
In interactive mode `prompt`, `done` and `checks` may be absent.
`net_allow` is optional and defaults to cuvm/data/allow-run.txt.
`browser` (webkit or chromium) and `browser_headless` are optional (docs/config.md).
`on_refusal` (stop or deny) and `refusal_reprompt` are optional, scripted only (the deny-and-continue design).
`project` (a .tgz in inputs) and `dev_server` ({command, port, ready_s}) are optional (the web-dev design).
`tools` (built-in Claude Code tools the agent may use) is optional (the guest-tools design).
`agent` is claude or codex; codex (the codex design) has no computer use and takes only the model in agent_args.
"""
import hashlib
import json
import pathlib
import re

from cuvm import DATA, project, tools

KEYS = {
    "agent": str, "agent_args": list, "image": str, "plan": str,
    "inputs": str, "results": str, "deadline_s": int, "apps": list,
    "grants": list, "allow_fullscreen": bool, "prompt": str, "done": dict, "checks": list,
}
OPTIONAL = {"net_allow": str, "browser": str, "browser_headless": bool, "on_refusal": str, "refusal_reprompt": str,
            "project": str, "dev_server": dict, "tools": list}
DEV_KEYS = {"command": str, "port": int, "ready_s": int}
TOOL_FLAGS = ("--tools", "--disallowedTools", "--disallowed-tools", "--allowedTools", "--allowed-tools")
# With `tools`, agent_args is an allowlist: each of these takes exactly one value (plain or `=` form) and nothing
# else is accepted. Claude Code has many arguments that narrow or remove tools regardless of the installed
# settings file (--settings, --managed-settings, --setting-sources, --restricted, --project-config-root,
# --permission-mode plan, --agents with --agent, --plugin-dir hooks); a name-based denylist missed some in
# three review rounds (three Lookout rounds), so the harness owns everything but the model choice.
TOOLS_AGENT_ARGS = ("--model", "--effort", "--fallback-model")
AGENTS = ("claude", "codex")
# Codex: -c key=value, -a, -s, -p and the bypass flags would override the per-run config.toml (approval, sandbox,
# provider), so the harness owns everything but the model choice.
CODEX_AGENT_ARGS = ("--model", "-m")
CODEX_SWAP_HOST = "chatgpt.com"  # the proxy swaps the credential here; net_allow must list it
CODEX_REFUSED = ("tools", "on_refusal", "refusal_reprompt")  # Codex's sandbox and approval policy do the gating
REFUSAL = ("stop", "deny")
ENGINES = ("webkit", "chromium")
CHECKS = {"guest_file_equals": list, "browser_navigated": str}
SCRIPTED_ONLY = ("prompt", "done", "checks", "on_refusal", "refusal_reprompt")  # optional, and ignored, in interactive mode


class ConfigError(ValueError):
    pass


def _allowlisted_args(args, allowed, why):
    """agent_args as an allowlist: only `allowed` options, each at most once with exactly one value (plain or `=`
    form), nothing else. `why` opens the error message."""
    i, seen = 0, set()
    while i < len(args):
        opt, eq, val = args[i].partition("=")
        if opt not in allowed or opt in seen:
            raise ConfigError("%s, so agent_args may hold only %s (each once, with one value); got %r"
                              % (why, allowed, args[i]))
        seen.add(opt)
        if not eq:
            i += 1
            val = args[i] if i < len(args) else ""
        if not val or val.startswith("-"):
            raise ConfigError("%s needs one value in agent_args, got %r" % (opt, val))
        i += 1


def _tools_agent_args(args):
    """With `tools`, only TOOLS_AGENT_ARGS, each with one value; the harness passes every tool flag itself."""
    _allowlisted_args(args, TOOLS_AGENT_ARGS, "tools is set")


def _codex(c):
    """Codex (the codex design step 1): no computer use (approvals are Codex's own prompt), nothing to deny from the
    host, the harness owns every argument but the model, and the swap host must be reachable."""
    if c["apps"] or c["grants"] or c["allow_fullscreen"]:
        raise ConfigError("agent codex has no computer use: apps and grants must be [] and allow_fullscreen false")
    present = [k for k in CODEX_REFUSED if k in c]
    if present:
        raise ConfigError("agent codex does not take %s (Codex's sandbox and approval policy do the gating)" % present)
    _allowlisted_args(c["agent_args"], CODEX_AGENT_ARGS, "agent is codex")
    if "net_allow" not in c:
        raise ConfigError("agent codex needs net_allow, a file listing %s" % CODEX_SWAP_HOST)


def load(path, interactive=False):
    path = pathlib.Path(path).resolve()
    raw = path.read_bytes()
    try:
        c = json.loads(raw)
    except ValueError as e:
        raise ConfigError("not JSON: %s" % e)
    missing = sorted(set(KEYS) - set(c) - (set(SCRIPTED_ONLY) if interactive else set()))
    unknown = sorted(set(c) - set(KEYS) - set(OPTIONAL))
    if missing or unknown:
        raise ConfigError("missing %s, unknown %s" % (missing, unknown))
    for k, t in {**KEYS, **OPTIONAL}.items():
        if k in c and (not isinstance(c[k], t) or (t is int and isinstance(c[k], bool))):
            raise ConfigError("%s must be %s" % (k, t.__name__))
    if c["agent"] not in AGENTS:
        raise ConfigError("agent must be one of %s" % (AGENTS,))
    if not all(isinstance(a, str) for a in c["agent_args"]):
        raise ConfigError("agent_args must be a list of strings")
    if c["agent"] == "codex":
        _codex(c)
    if "tools" in c:
        t = c["tools"]
        if not t or not all(isinstance(x, str) for x in t) or len(set(t)) != len(t) or set(t) - set(tools.TOOLS):
            raise ConfigError("tools must be distinct names from %s: %r" % (tools.TOOLS, t))
        _tools_agent_args(c["agent_args"])
    if not re.fullmatch(r"~/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*", c["results"]) or ".." in c["results"].split("/"):
        raise ConfigError("results must be a guest path under ~/ (letters, digits, . _ -): %r" % c["results"])
    if not c["apps"] and c["agent"] != "codex":
        raise ConfigError("apps must list at least one app")
    if "browser" in c and c["browser"] not in ENGINES:
        raise ConfigError("browser must be one of %s" % (ENGINES,))
    if "on_refusal" in c and c["on_refusal"] not in REFUSAL:
        raise ConfigError("on_refusal must be one of %s" % (REFUSAL,))
    if "refusal_reprompt" in c and c.get("on_refusal") != "deny":
        raise ConfigError("refusal_reprompt needs on_refusal: deny")
    if "refusal_reprompt" in c and not c["refusal_reprompt"].strip():
        raise ConfigError("refusal_reprompt must not be empty")
    if "browser_headless" in c and "browser" not in c:
        raise ConfigError("browser_headless needs browser")
    if "project" in c and not project.ARCHIVE.fullmatch(c["project"]):
        raise ConfigError("project is the name of a .tgz in inputs (letters, digits, . _ -): %r" % c["project"])
    if "project" in c and (c["results"] == project.GUEST_DIR or c["results"].startswith(project.GUEST_DIR + "/")):
        raise ConfigError("results must not be under %s with project (harness output would read as project changes)"
                          % project.GUEST_DIR)
    if "dev_server" in c:
        if "project" not in c or "browser" not in c:
            raise ConfigError("dev_server needs project and browser")
        d = c["dev_server"]
        if set(d) - set(DEV_KEYS) or {"command", "port"} - set(d):
            raise ConfigError("dev_server has command, port and optional ready_s: %s" % sorted(d))
        for k, t in DEV_KEYS.items():
            if k in d and (not isinstance(d[k], t) or (t is int and isinstance(d[k], bool))):
                raise ConfigError("dev_server.%s must be %s" % (k, t.__name__))
        if not d["command"].strip():
            raise ConfigError("dev_server.command must not be empty")
        if not 1024 <= d["port"] <= 65535:
            raise ConfigError("dev_server.port must be 1024 to 65535")
        if d.setdefault("ready_s", 60) < 1:
            raise ConfigError("dev_server.ready_s must be at least 1")
    for chk in c.get("checks", []):
        kind = list(chk) if isinstance(chk, dict) else []
        if len(kind) != 1 or kind[0] not in CHECKS or not isinstance(chk[kind[0]], CHECKS[kind[0]]):
            raise ConfigError("each check is one of %s: %r" % (sorted(CHECKS), chk))
        if kind[0] == "guest_file_equals" and not (len(chk[kind[0]]) == 2 and all(isinstance(x, str) for x in chk[kind[0]])):
            raise ConfigError("guest_file_equals is [path, expected]: %r" % chk)
        if kind[0] == "browser_navigated" and "browser" not in c:
            raise ConfigError("browser_navigated needs browser")
        if kind[0] == "browser_navigated" and not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", chk[kind[0]]):
            raise ConfigError("browser_navigated is a lowercase host name, no scheme, port or path: %r" % chk)
    c["inputs"] = str(path.parent / c["inputs"])
    c["net_allow"] = str(path.parent / c["net_allow"]) if "net_allow" in c else str(DATA / "allow-run.txt")
    if c["agent"] == "codex":
        try:
            hosts = pathlib.Path(c["net_allow"]).read_text().split()
        except OSError as e:
            raise ConfigError("net_allow file unreadable: %s" % e)
        if CODEX_SWAP_HOST not in hosts:
            raise ConfigError("agent codex needs %s in net_allow %s" % (CODEX_SWAP_HOST, c["net_allow"]))
    if not (pathlib.Path(c["inputs"]) / "known.txt").is_file():
        raise ConfigError("inputs dir needs known.txt (probe F1 reads it): %s" % c["inputs"])
    if "project" in c:
        arc = pathlib.Path(c["inputs"]) / c["project"]
        if not arc.is_file():
            raise ConfigError("project archive missing: %s" % arc)
        try:
            c["project_top"] = project.validate(arc)
        except project.ProjectError as e:
            raise ConfigError("project archive %s: %s" % (c["project"], e))
    return c, hashlib.sha256(raw).hexdigest()
