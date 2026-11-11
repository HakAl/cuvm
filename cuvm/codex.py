"""Codex CLI as a guest agent (the codex design): the Codex specifics, pure functions over config, image, gateway
and port. Scripted runs are one `codex exec --json` turn under approval `never`; interactive runs are the TUI
under `on-request`. The credential is a per-run dummy JWT in ~/.codex/auth.json; the egress proxy swaps the real
access token in at chatgpt.com. Nothing here ever answers a Codex prompt.

cuvm/config.py must not import this module (browser imports config; this imports browser and session).
"""
import base64
import datetime
import json
import re
import secrets
import shlex
import time
import urllib.parse

from cuvm import browser, config as cfgmod, dialog, transcript

VERSION = "0.167.0"
CODEX_KEYS = ("bin", "version", "binary", "binary_sha256")  # the image record's codex block
SWAP_HOST = cfgmod.CODEX_SWAP_HOST
KEYCHAIN_ITEM = "cuvm-codex-auth"
MODES = ("scripted", "interactive")
ISSUER = "https://auth.openai.com"
AUDIENCE = "https://api.openai.com/v1"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # the public Codex client id seen in the probe, not a secret
REFRESH_PLACEHOLDER = "cuvm-no-refresh-token"  # the field is required by the deserializer; a refresh goes to auth.openai.com, which the allowlist refuses
TOKEN_MAX = 400  # the real access token is 1805 characters with a payload over 1000; E2 tells them apart by payload length
CA_ENV = "CODEX_CA_CERTIFICATE=$HOME/.cuvm/ca.pem"
TOOL_NAMES = ("exec_command", "custom:exec", "apply_patch")  # Codex's own tools are always on
PROMPT_KINDS = ("codex_command", "codex_patch", "unknown")  # what the interactive Observer records for codex
# Completed item types that are not tool calls (codex exec --json / rollouts); any other type the judges do not
# know becomes item:<type> and is forbidden (L5 deviation 5: the backend offers tools the harness never saw).
NON_TOOL_ITEMS = ("agent_message", "reasoning", "error", "todo_list")  # exec_events.rs 107 to 117; todo_list is a plan
NON_TOOL_ROLLOUT_ITEMS = ("UserMessage", "AgentMessage", "Reasoning")
# Files of a scripted run, under ~/.cuvm (pruned from the home capture, removed at teardown).
EVENTS, LAST, STDERR, EXIT = "~/.cuvm/codex-events.jsonl", "~/.cuvm/codex-last.txt", "~/.cuvm/codex-stderr.txt", "~/.cuvm/codex-exit.txt"
REACH_PROMPT = "Reply with exactly PONG"

# TUI markers (tests/fixtures/codex/, cut out of the probe transcripts)
READY = (">_ OpenAI Codex (v", "› Ask Codex to do anything")
BUSY = "esc to interrupt"
STATUS = " · ~/"
FOOTER = "Press enter to confirm or esc to cancel"
START = "Would you like to run the following command?"
START_PATCH = "Would you like to make the following edits?"  # L5 dialog 2 (tui-patch-approval.txt)
OPTIONS = (re.compile(r"^(› ?)?1\. Yes, proceed \(y\)$"),
           re.compile(r"^(› ?)?2\. Yes, and don't ask again for commands that start with .+ \(p\)$"),
           re.compile(r"^(› ?)?3\. No, and tell Codex what to do differently \(esc\)$"))
PATCH_OPTIONS = (OPTIONS[0], re.compile(r"^(› ?)?2\. Yes, and don't ask again for these files \(a\)$"), OPTIONS[2])
OPTION_LINE = re.compile(r"^(› ?)?[123]\. ")
UNKNOWN_LINES = 15

# Rollouts live at ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (the shape of run.TRANSCRIPTS).
TRANSCRIPTS = ('h="$HOME"; c="$h/.codex"; d="$c/sessions"; '
               'no() { echo "transcripts: $1" >&2; exit 2; }; '
               '{ [ -d "$h" ] && [ -x "$h" ]; } || no "home not a searchable directory"; '
               'if [ ! -e "$c" ] && [ ! -L "$c" ]; then exit 0; fi; '
               '{ [ -d "$c" ] && [ -x "$c" ]; } || no "~/.codex not a searchable directory"; '
               'if [ ! -e "$d" ] && [ ! -L "$d" ]; then exit 0; fi; '
               '[ -d "$d" ] || no "~/.codex/sessions not a directory"; '
               'find -L "$d" -mindepth 4 -maxdepth 4 -name "rollout-*.jsonl"')

# E2 for codex (zsh over ssh): the dummy and the placeholder are under TOKEN_MAX in total, the real token's
# payload is over 1000 characters, so a payload of 400 or more is a real token on disk.
# BSD grep (the guest's /usr/bin/grep) rejects a repetition count over 255 ("maximum repetition exceeds 255",
# exit 2), so 400 or more is written as 200 plus 200 or more. The binary is pinned by path.
JWT_LONG = r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{200}[A-Za-z0-9_-]{200,}"


def home(image):
    return "/Users/%s" % image["guest_user"]


def check_image(cfg, image):
    """The image record must carry the codex block at the pinned version; called once the record is loaded."""
    if cfg.get("agent") != "codex":
        return
    b = image.get("codex") or {}
    missing = [k for k in CODEX_KEYS if not b.get(k)]
    if missing:
        raise cfgmod.ConfigError("agent codex needs an image record with a codex block (missing %s)" % missing)
    if b["version"] != VERSION:
        raise cfgmod.ConfigError("image has codex %s, this harness supports %s only" % (b["version"], VERSION))


def tool_rules(cfg):
    """(allowed prefixes, allowed names, denied names) for the codex judges. Without `browser` every `mcp__`
    name is forbidden; with it only the Playwright prefix minus the denied three is ordinary. browser.tool_rules
    is not used: it allows mcp__computer-use__ and would class exec_command as forbidden (CP2-1)."""
    if "browser" not in cfg:
        return (), TOOL_NAMES, ()
    return (browser.PREFIX,), TOOL_NAMES, browser.DENIED


def _toml(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (str, list)):
        return json.dumps(v)  # JSON strings and arrays of strings are valid TOML for these values
    raise TypeError("no TOML form for %r" % (v,))


def config_toml(cfg, image, gateway, port, mode):
    """~/.codex/config.toml for the run. Top-level keys first, then tables (TOML requires that order)."""
    if mode not in MODES:
        raise ValueError("mode must be one of %s, got %r" % (MODES, mode))
    top = [("approval_policy", "never" if mode == "scripted" else "on-request"),
           ("sandbox_mode", "workspace-write"),
           ("check_for_update_on_startup", False),
           ("web_search", "disabled")]  # the backend's web tool is not offered (L5 deviation 5)
    tables = [("sandbox_workspace_write", [("writable_roots", ["%s/Desktop" % home(image)]), ("network_access", True)]),
              ("analytics", [("enabled", False)]),
              ("otel", [("metrics_exporter", "none"), ("trace_exporter", "none")]),
              ("history", [("persistence", "save-all")]),
              # the ChatGPT backend's own tools run outside the guest and so outside net_allow: off (L5 deviation 5;
              # Stable feature keys in the 0.167.0 sources, default true except multi_agent_v2)
              ("features", [("enable_request_compression", False), ("apps", False), ("plugins", False),
                            ("image_generation", False), ("multi_agent", False), ("multi_agent_v2", False)]),
              # L7 deviation 7: multi_agent alone left the collaboration tools on and the turn ran multi-agent v2;
              # multi_agent_v2 is the v2 feature key (features-lib.rs 1598) and [agents] enabled the tools switch
              # (config_toml.rs 766 to 769, "an enabled features.multi_agent_v2 setting takes precedence")
              ("agents", [("enabled", False)]),
              ("tui", [("screen_reader_detection_done", True)])]
    if "browser" in cfg:
        pw = browser.mcp_config(cfg, image, gateway, port)["mcpServers"]["playwright"]
        tables.append(("mcp_servers.playwright", [
            ("command", pw["command"]), ("args", pw["args"]),
            ("disabled_tools", [d[len(browser.PREFIX):] for d in browser.DENIED]),
            ("default_tools_approval_mode", "approve")]))
    out = ["# written by cuvm for one run; Codex reads it at start (the codex design step 2)"]
    out += ["%s = %s" % (k, _toml(v)) for k, v in top]
    for name, keys in tables:
        out += ["", "[%s]" % name] + ["%s = %s" % (k, _toml(v)) for k, v in keys]
    return "\n".join(out) + "\n"


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt(run_id, sub):
    """A JWT-shaped string Codex deserializes but no server accepts: unsigned claims, random signature.
    No account or plan claim (the account id is published by the proxy later). Under TOKEN_MAX in total, so
    E2's payload-length rule never matches it. The claim set is the real token's minus everything secret or
    identifying; nbf and kid are left out and the nonce is 16 bytes so a run id of 50 characters still fits."""
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}  # the standard header; sub marks the dummy
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 2 * 365 * 86400,
               "cuvm": {"run": run_id, "nonce": _b64(secrets.token_bytes(16))}}
    tok = ".".join([_b64(json.dumps(header, separators=(",", ":")).encode()),
                    _b64(json.dumps(payload, separators=(",", ":")).encode()),
                    _b64(secrets.token_bytes(32))])
    if len(tok) >= TOKEN_MAX:
        raise ValueError("dummy token would be %d characters (limit %d): run id too long" % (len(tok), TOKEN_MAX))
    return tok


def dummy_token(run_id):
    """The access token the guest holds; the proxy swaps the real one in at SWAP_HOST."""
    return _jwt(run_id, "cuvm-dummy")


def placeholder_id_token(run_id):
    """The id token field of auth.json; the proxy never sees it and it never equals the dummy."""
    return _jwt(run_id, "cuvm-dummy-id")


def auth_json(dummy, id_token, account_id):
    """The text of ~/.codex/auth.json for the run (injected over ssh stdin, never argv)."""
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    d = {"OPENAI_API_KEY": None, "auth_mode": "chatgpt", "last_refresh": now,
         "tokens": {"access_token": dummy, "account_id": account_id, "id_token": id_token,
                    "refresh_token": REFRESH_PLACEHOLDER, "login_owner_id": "cuvm-dummy",
                    "oauth_provenance": {"issuer": ISSUER, "client_id": CLIENT_ID}}}
    return json.dumps(d, indent=2) + "\n"


def env(gateway, port):
    """The proxy variables plus the CA for Codex's own TLS stack (Node's variable does not reach the native binary)."""
    from cuvm import session  # session imports this module; the proxy variables are its
    return "%s %s" % (session.env(gateway, port), CA_ENV)


def tui_cmd(gateway, port, args):
    """Interactive: the TUI in the tmux pane. No keychain unlock: the credential is a file."""
    return ("%s exec codex %s" % (env(gateway, port), " ".join(shlex.quote(a) for a in args))).rstrip()


def exec_cmd(gateway, port, args, prompt):
    """Scripted: one `codex exec --json` turn; events, last message, stderr and exit status go to files under
    ~/.cuvm. stdin is closed so Codex never waits on it (exec-hang.txt). The exit file is created empty by the
    shell before the echo lands, so a watcher must test its size, not its existence."""
    extra = "".join(shlex.quote(a) + " " for a in args)
    return ("cd ~/exp && %s codex exec --skip-git-repo-check --json -o %s %s%s </dev/null > %s 2> %s; "
            "echo \"EXIT=$?\" > %s" % (env(gateway, port), LAST, extra, shlex.quote(prompt), EVENTS, STDERR, EXIT))


def reach_cmd(gateway, port):
    """The reach check: an ephemeral exec turn; pass when ~/reach.txt is exactly PONG."""
    return ("cd ~/exp && %s codex exec --skip-git-repo-check --ephemeral --json -o ~/reach.txt %s </dev/null "
            "> ~/reach-events.jsonl 2>&1" % (env(gateway, port), shlex.quote(REACH_PROMPT)))


# TUI state (interactive runs and the ready wait)
def ready(pane):
    return all(s in pane for s in READY)


def idle(pane):
    """Codex waits for input: no work in progress, no prompt open, and the last line starting with the chevron
    is followed within three lines by the status line (true on the ready and complete captures, false on the
    approval prompt, whose last chevron line is an option)."""
    if BUSY in pane or parse_prompt(pane) is not None:
        return False
    lines = pane.splitlines()
    chev = [i for i, l in enumerate(lines) if l.startswith("›")]
    if not chev:
        return False
    i = chev[-1]
    return any(STATUS in l for l in lines[i + 1:i + 4])


def parse_prompt(pane):
    """The open Codex approval prompt as a dialog.Dialog, or None without the footer. A command prompt is kind
    codex_command with extra [command, reason]; a patch prompt (L5) is kind codex_patch with extra [destination,
    description]; any other footer block is kind unknown with the non-empty lines above the footer as text.
    Lines the TUI wrapped at the terminal width are rejoined (L5 deviation 4). Nothing answers it from the host."""
    lines = pane.splitlines()
    foot = [i for i, l in enumerate(lines) if FOOTER in l]
    if not foot:
        return None
    end = foot[-1]
    starts = [i for i in range(end) if lines[i].strip() in (START, START_PATCH)]  # whole lines (agy CL5-3)
    if starts:
        block = [l.strip() for l in lines[starts[-1]:end + 1] if l.strip()]
        d = _command(block) if block[0] == START else _patch(block)
        if d is not None:
            return d
    above = [l.strip() for l in lines[:end] if l.strip()][-UNKNOWN_LINES:]
    return dialog.Dialog("unknown", text="\n".join(above))


def _command(block):
    """block: START, Environment:, Reason: (continuation lines allowed), one `$ ` line (wrapped continuation lines
    allowed up to the first option), the three options in order (each may wrap) with the cursor on at most one of
    them, FOOTER. Anything else is not this grammar."""
    if len(block) < 8 or block[0] != START or FOOTER not in block[-1]:
        return None
    body = block[1:-1]
    if not body[0].startswith("Environment:") or not body[1].startswith("Reason:"):
        return None
    cmds = [i for i, l in enumerate(body) if l.startswith("$ ")]
    if len(cmds) != 1:
        return None
    reason = " ".join([body[1][len("Reason:"):].strip()] + body[2:cmds[0]]).strip()
    first = [i for i in range(cmds[0] + 1, len(body)) if OPTION_LINE.match(body[i])]
    if not first:
        return None
    command = body[cmds[0]][2:].strip() + "".join(body[cmds[0] + 1:first[0]])
    opts = _options(body[first[0]:])
    if not _three(opts, OPTIONS):
        return None
    return dialog.Dialog("codex_command", extra=[command, reason], text="\n".join(block))


def _patch(block):
    """block: START_PATCH, Description:, Destination: (wrapped continuation lines allowed up to the first option),
    the three patch options in order with the cursor on at most one of them, FOOTER (L5 dialog 2)."""
    if len(block) < 7 or block[0] != START_PATCH or FOOTER not in block[-1]:
        return None
    body = block[1:-1]
    if not body[0].startswith("Description:") or not body[1].startswith("Destination:"):
        return None
    first = [i for i in range(2, len(body)) if OPTION_LINE.match(body[i])]
    if not first:
        return None
    dest = body[1][len("Destination:"):].strip() + "".join(body[2:first[0]])
    opts = _options(body[first[0]:])
    if not _three(opts, PATCH_OPTIONS):
        return None
    return dialog.Dialog("codex_patch", extra=[dest, body[0][len("Description:"):].strip()], text="\n".join(block))


def _options(lines):
    """Option lines as the TUI printed them, a line that does not start a new option appended to the one before it
    without a separator (a hard wrap at the terminal width; L5 dialog 1 wrapped option 2 inside a path)."""
    out = []
    for l in lines:
        if OPTION_LINE.match(l) or not out:
            out.append(l)
        else:
            out[-1] += l
    return out


def _three(opts, patterns):
    return len(opts) == 3 and all(rx.match(l) for rx, l in zip(patterns, opts)) and sum(l.startswith("›") for l in opts) <= 1


# judges
def _mcp_name(it):
    return "mcp__%s__%s" % (it.get("server") or "?", it.get("tool") or "?")


def _forbidden(names, allowed_prefixes, allowed_names, denied_names):
    return [t for t in names if t in denied_names or (t not in allowed_names and not t.startswith(tuple(allowed_prefixes)))]


def _texts(it):
    content = (it.get("result") or {}).get("content") if isinstance(it.get("result"), dict) else None
    return [p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text"]


def _message(e):
    err = e.get("error")
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    if e.get("message"):
        return str(e["message"])
    return json.dumps({k: v for k, v in e.items() if k != "type"})[:500]


def extract(lines, allowed_prefixes, allowed_names, denied_names=()):
    """Mechanical extraction from a `codex exec --json` event stream. tools_used comes from item.completed items
    of type command_execution (as exec_command), mcp_tool_call (as mcp__<server>__<tool>), file_change (as
    apply_patch) and any other type but NON_TOOL_ITEMS (as item:<type>, forbidden; L5 deviation 5). Messages,
    reasoning, todo_list and error items are counted in `items` by type and never enter tools_used; an error
    item's message joins error_events. A forbidden item is used when its status is completed or absent
    (web_search carries none, exec_events.rs 298; agy CL5-1), attempted otherwise. The computer-use fields keep their names with empty or
    null values so the record keeps its shape; no computer-use rule applies to codex."""
    used, status, navs, cerr, nfail, items, unparsed = [], {}, [], 0, 0, {}, 0
    thread, last_msg, failed, usages = None, None, [], []
    for line in lines:
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            if not isinstance(e, dict):
                raise ValueError
        except ValueError:
            unparsed += 1
            continue
        t = e.get("type")
        if t == "thread.started":
            thread = e.get("thread_id") or thread
        elif t == "turn.completed":
            usages.append(e.get("usage") if isinstance(e.get("usage"), dict) else {})
        elif t in ("turn.failed", "error"):
            failed.append(_message(e))
        elif t == "item.completed" and isinstance(e.get("item"), dict):
            it = e["item"]
            kind = it.get("type") or "?"
            items[kind] = items.get(kind, 0) + 1
            if kind == "agent_message":
                last_msg = str(it.get("text") or "")[:2000]
                continue
            if kind == "command_execution":
                name = "exec_command"
            elif kind == "mcp_tool_call":
                name = _mcp_name(it)
            elif kind == "file_change":
                name = "apply_patch"
            elif kind == "error":
                failed.append(_message(it))
                continue
            elif kind in NON_TOOL_ITEMS:
                continue
            else:
                name = "item:%s" % kind  # a tool the judge cannot vouch for (L5 deviation 5)
            ok = it.get("status") in (None, "completed")  # web_search items have no status (agy CL5-1)
            used.append(name)
            status[name] = status.get(name, False) or ok
            if ok and name == transcript.NAVIGATE:
                args = it.get("arguments") if isinstance(it.get("arguments"), dict) else {}
                host = urllib.parse.urlsplit(str(args.get("url", ""))).hostname
                if host:
                    navs.append(host)
            if ok and name == browser.PREFIX + "browser_console_messages":
                cerr += transcript.console_errors(_texts(it))
            if ok and name == browser.PREFIX + "browser_network_requests":
                nfail += transcript.network_failed(_texts(it))
    tools = sorted(set(used))
    forbidden = _forbidden(tools, allowed_prefixes, allowed_names, denied_names)
    total = {}
    for u in usages:
        for k, v in u.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                total[k] = total.get(k, 0) + v
    return {"tools_used": tools, "forbidden_tools_attempted": forbidden,
            "forbidden_tools_used": [t for t in forbidden if status.get(t)],
            "request_access_granted": [], "input_tool_ok_before_final_shot": None,
            "final_image_sha256": None, "final_image_is_error": None, "final_image_media_type": None, "final_image_bytes": None,
            "browser_navigated_hosts": sorted(set(navs)), "browser_hosts_before_input": sorted(set(navs)),
            "browser_console_errors": cerr, "browser_network_failed": nfail,
            "items": items, "unparsed_lines": unparsed,
            "codex_usage": {"last": usages[-1] if usages else None, "sum": total, "turns": len(usages)},
            "codex_thread_id": thread, "codex_last_message": last_msg, "error_events": failed}


def extract_rollout(lines, allowed_prefixes, allowed_names, denied_names=()):
    """Reduced extraction from a rollout file (interactive runs). tools_used from event_msg item_completed items
    of type CommandExecution (exec_command), McpToolCall (mcp__<server>__<tool>; the shape observed in the L3
    run: server, tool, arguments, readOnlyHint, status, result; tests/fixtures/codex/rollout-web-items.jsonl),
    FileChange (apply_patch) or Extension (extension:<kind>; the backend's web search in the L5 run,
    tests/fixtures/codex/rollout-l5-items.jsonl), any other type but the messages as item:<type>, and from
    response_item custom_tool_call names (custom:<name>). codex_usage is the last token_count payload (running
    totals, not summed; rollout-tui.txt has two for one turn; L5 shape: info.total_token_usage, info.last_token_usage,
    info.model_context_window, rate_limits; type, thread_id and turn_id dropped)."""
    used, status, usage, unparsed = [], {}, None, 0
    for line in lines:
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            if not isinstance(r, dict):
                raise ValueError
        except ValueError:
            unparsed += 1
            continue
        p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        pt = p.get("type")
        name = None
        if r.get("type") == "event_msg" and pt == "item_completed" and isinstance(p.get("item"), dict):
            it = p["item"]
            if it.get("type") == "CommandExecution":
                name = "exec_command"
            elif it.get("type") == "McpToolCall":
                name = _mcp_name(it)
            elif it.get("type") == "FileChange":
                name = "apply_patch"
            elif it.get("type") == "Extension":
                name = "extension:%s" % (it.get("kind") or "?")
            elif it.get("type") not in NON_TOOL_ROLLOUT_ITEMS:
                name = "item:%s" % it.get("type")  # a tool the judge cannot vouch for (L5 deviation 5)
            st = it.get("status")
        elif r.get("type") == "event_msg" and pt == "token_count":
            usage = {k: p[k] for k in ("info", "rate_limits") if k in p}  # type, thread_id, turn_id dropped
            continue
        elif r.get("type") == "response_item" and pt == "custom_tool_call":
            name = "custom:%s" % (p.get("name") or "?")
            st = p.get("status")
        if name is None:
            continue
        used.append(name)
        status[name] = status.get(name, False) or st in (None, "completed")
    tools = sorted(set(used))
    forbidden = _forbidden(tools, allowed_prefixes, allowed_names, denied_names)
    return {"tools_used": tools, "forbidden_tools_attempted": forbidden,
            "forbidden_tools_used": [t for t in forbidden if status.get(t)],
            "request_access_granted": [], "browser_navigated_hosts": [],
            "browser_console_errors": 0, "browser_network_failed": 0,
            "unparsed_lines": unparsed, "codex_usage": usage}


def sum_usage(parts):
    """The per-file token_count payloads of collect_interactive as one: the last file's payload with
    info.total_token_usage summed over all files. Nothing else is additive (L5: model_context_window and the
    rate_limits percentages are states, not counts)."""
    parts = [p for p in parts if isinstance(p, dict)]
    if not parts:
        return {}
    out = json.loads(json.dumps(parts[-1]))
    total = _sum_leaves([p["info"].get("total_token_usage") for p in parts if isinstance(p.get("info"), dict)])
    if total:
        if not isinstance(out.get("info"), dict):  # info is Option<TokenUsageInfo> (protocol.rs 2584); Lookout R6-F1
            out["info"] = {}
        out["info"]["total_token_usage"] = total
    return out


def _sum_leaves(parts):
    out = {}
    for p in parts:
        if not isinstance(p, dict):
            continue
        for k, v in p.items():
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                out[k] = out.get(k, 0) + v
            elif isinstance(v, dict):
                out[k] = _sum_leaves([out.get(k, {}), v])
    return out


# E2, E3 and the login removal (zsh over ssh; one line per check, exit non-zero on any failure, never a secret)
def e2_cmd():
    return (
        "fail=0; "
        "if [ -e ~/.codex/auth.json ]; then echo 'E2.1 FAIL ~/.codex/auth.json present'; fail=1; "
        "else echo 'E2.1 ok ~/.codex/auth.json absent'; fi; "
        "hits=$(/usr/bin/grep -a -rEl --exclude-dir=node_modules '%s' /dev/null ~/.codex(N) ~/exp(N) ~/.zsh*(N) /etc/zshenv(N) </dev/null 2>/dev/null); st=$?; "
        "if [ $st -gt 1 ]; then echo \"E2.2 FAIL scan incomplete (grep exit $st)\"; fail=1; fi; "
        "if [ $st -eq 0 ] || [ -n \"$hits\" ]; then echo 'E2.2 FAIL token-shaped content on disk'; "
        "for h in ${(f)hits}; do echo \"HIT: $h\"; done; fail=1; "
        "elif [ $st -eq 1 ]; then echo 'E2.2 ok no long JWT under ~/.codex ~/exp ~/.zsh* /etc/zshenv'; fi; "
        "if [ -n \"${OPENAI_API_KEY-}\" ] || [ -n \"${CODEX_API_KEY-}\" ]; then echo 'E2.3 FAIL an API key variable is set'; fail=1; "
        "else echo 'E2.3 ok OPENAI_API_KEY and CODEX_API_KEY unset'; fi; "
        "exit $fail" % JWT_LONG)


def e3_cmd():
    """sha256 of tokens.access_token in ~/.codex/auth.json, printed; never the token."""
    return ("python3 -c 'import hashlib,json,os; "
            "print(hashlib.sha256(json.load(open(os.path.expanduser(\"~/.codex/auth.json\")))[\"tokens\"][\"access_token\"]"
            ".encode()).hexdigest())'")


def remove_cmd():
    return "rm -f ~/.codex/auth.json; " + e2_cmd()
