# Experiment config

One JSON file per experiment. Unknown keys and wrong types are rejected. The
file's sha256 goes into every run record, so a record always says exactly which
config produced it. Relative paths resolve against the config file's directory.

```json
{
  "agent": "claude",
  "agent_args": ["--tools", "ToolSearch", "--disallowedTools", "Bash,Read,Write,Edit,NotebookEdit,Glob,Grep"],
  "image": "cuvm-golden",
  "plan": "max",
  "inputs": "inputs",
  "results": "~/exp/results",
  "deadline_s": 600,
  "apps": ["TextEdit"],
  "grants": [],
  "allow_fullscreen": false,
  "prompt": "Open TextEdit ... type exactly SMOKE-{run_id}-OK ...",
  "done": {"guest_file": "~/Desktop/smoke.txt"},
  "checks": [{"guest_file_equals": ["~/Desktop/smoke.txt", "SMOKE-{run_id}-OK"]}]
}
```

| key | meaning |
| --- | --- |
| `agent` | `claude` or `codex` (Codex CLI 0.167.0 as the guest agent, the Codex section below). With `codex`: `apps` and `grants` must be `[]` and `allow_fullscreen` false (Codex has no computer use here; its approval prompt is its own control), `tools`, `on_refusal` and `refusal_reprompt` are refused (Codex's sandbox and approval policy do the gating), `agent_args` may hold only the model, `net_allow` is required and must list `chatgpt.com`, and the image record needs a `codex` block at version 0.167.0 (docs/image.md, Derived codex image) |
| `agent_args` | arguments to `claude` in the guest. `--tools ToolSearch` keeps only ToolSearch from the built-in tools (the computer-use MCP tools load through it); keep it unless the experiment is about tools. With `tools` this is an allowlist: only `--model`, `--effort` and `--fallback-model`, each with exactly one value (plain or `=` form); anything else is refused, including every tool flag. Claude Code has many arguments that narrow or remove tools whatever the installed settings file says (`--settings`, `--managed-settings`, `--setting-sources`, `--restricted`, `--project-config-root`, `--permission-mode plan`, `--agents` with `--agent`, `--plugin-dir` hooks), so the harness owns everything but the model choice. With `agent: codex` only `--model` or `-m` with exactly one value: `-c key=value`, `-a`, `-s`, `-p` and the bypass flags would override the per-run `config.toml` |
| `tools` | optional; built-in Claude Code tools the agent may use in the guest, distinct names from `Bash`, `Read`, `Write`, `Edit`, `NotebookEdit`, `Glob`, `Grep`. The image's settings deny them all and a deny rule beats every flag, so the run writes a per-run `~/.claude/settings.json` into the clone (the image's file with these names moved from deny to allow; the image itself is unchanged and the clone is deleted after the run), passes `--tools ToolSearch,<tools>` to `claude`, and counts them as ordinary in the transcript judge. The record has `tools`, `guest.settings_baseline_sha256` (the image's file) and `guest.settings_sha256` (the installed file). With a shell or file tools the agent writes files and runs processes as the guest user, including its own settings file, so the deny list for the other tools is then a default, not a boundary. A tool outside the config that appears in the transcript still fails the run, but the transcript is written in the guest and the same agent can edit it before collection, so the judge is evidence, not enforcement. The run verifies only `~/.claude/settings.json`: a managed settings file (`/Library/Application Support/ClaudeCode/managed-settings.json`) would still win over the installed one; the image has none and the run does not check the clone for one. The egress allowlist, the dummy login and the deleted clone do not depend on the tools (docs/design.md). The dev server runs in its own tmux server and a shell can stop it; D1 is checked before the agent only |
| `image` | name of the golden VM; its record must exist in `images/<image>.json` |
| `plan` | `max` or `pro`, written into the injected login; computer use needs a paid plan. With `codex` it is recorded only (Codex takes the plan from the account) |
| `inputs` | host directory shared read-only into the guest at `/Volumes/My Shared Files/inputs`. Must contain `known.txt` (probe F1 hashes it) |
| `results` | guest directory the agent may write to, a path under `~/` (letters, digits, `.`, `_`, `-`); its contents are collected as `results.tgz`. The golden image has `~/exp/results`; another directory must exist in the image or be created by the agent. A failed archive or copy fails the run |
| `deadline_s` | wall-clock limit for the agent phase; the run ends `deadline` and FAILs after it. Interactive: the session's time limit (warning five minutes before; recorded, not a FAIL) |
| `net_allow` | optional; file of hostnames the proxy forwards (fnmatch patterns, one per line). Default `cuvm/data/allow-run.txt`: `api.anthropic.com`, `claude.ai`, `platform.claude.com`, `claude.com`. Required with `agent: codex`, and the file must list `chatgpt.com`, the host the proxy swaps the credential at |
| `browser` | optional; `webkit` (or `chromium` if the image has it). Starts a Playwright MCP server in the guest (`mcp__playwright__*` tools), pinned and hash-checked by the image record's `browser` block (probe B1). Its traffic uses the run's proxy and `net_allow`. `browser_install`, `browser_file_upload` and `browser_run_code_unsafe` are denied and count as forbidden tools. Needs an image built per docs/image.md, Derived browser image. Several allowed Playwright tools take a `filename` and write their output under the agent's working directory (`~/exp`) or the browser output directory (`<results>/browser`), so with `browser` keep `done` and `guest_file_equals` targets outside both. With `codex` the server is an MCP table in the per-run `config.toml` and the three denied tools are its `disabled_tools`, never offered to the model (Codex section) |
| `browser_headless` | optional, with `browser`; default false (the browser window shows in the Tart window) |
| `project` | optional; file name of a `.tgz` in `inputs` (letters, digits, `.`, `_`, `-`) holding exactly one top-level directory (plain name; no absolute, `.`, `..` or empty member name parts; no backslash or control character in a name; names, link targets and resolved link paths at most 1024 characters; symlinks, followed through each other, must stay within that directory, have relative targets, and nothing may lie under a symlink; checked at load, each link resolved once with a work cap). `results` must not be under `~/exp/project`. Unpacked into `~/exp/project/` in the guest, writable. Build it with `python3 -m cuvm project pack DIR OUT.tgz` (excludes `.git` and `.DS_Store`, keeps `node_modules`, stores a hard link as a plain copy, refuses an `OUT.tgz` that is `DIR` or inside it (judged by directory identity, so a case alias, a symlinked parent or a mount alias counts, and the walk refuses a file that turns out to be the archive being written; the `.part` file is created exclusively without following links, so a stale or planted `OUT.tgz.part` is refused and must be removed first; the output directory is single-writer while pack runs: the archive is validated through pack's own descriptor and is published, or on failure removed, only if the `.part` name still points at the file pack created), a tree containing `sk-ant-` or a symlink that is absolute or resolves outside the tree, and validates the archive with the same rules as loading before writing it). After the run the record has `project.patch` (diff against a clean unpack, `node_modules` excluded), `project-changed.tgz` (added and modified files) and the added, modified and deleted lists |
| `dev_server` | optional, needs `project` and `browser`: `{"command": ..., "port": N, "ready_s": N}`. The harness runs `command` in `~/exp/project/<top dir>` in its own tmux server (socket `dev`) with the session environment, before the agent starts, and waits up to `ready_s` (default 60) for `http://127.0.0.1:<port>/` to answer 200. The command must bind loopback explicitly (for Vite: `--host 127.0.0.1 --port N --strictPort`): probe D1 fails the run unless every listener on `port` is `127.0.0.1` or `[::1]`. The Playwright server gets `--proxy-bypass localhost,127.0.0.1`, so the browser reaches the dev server directly and everything else still goes through the proxy; probe D2 fails the run if the proxy refused any loopback request while the agent ran. `port` is 1024 to 65535. Output goes to `<results>/dev-server.log`; the dev tmux pane is saved as `dev-server-pane.txt` |
| `apps` | apps the harness may approve in the access dialog. At least one, except with `agent: codex`, where it must be empty |
| `grants` | extra grant flags the harness may approve: `clipboardRead`, `clipboardWrite`, `systemKeyCombos`. Default none |
| `allow_fullscreen` | intended to allow the full-screen takeover card. In v0.1 the card is always refused (its text has not been captured, so there is nothing to check it against) |
| `on_refusal` | optional, scripted only. What the harness does with an access dialog that asks for apps or grants outside the config. `stop` (default): leave it open and end the run `blocked_on_approval`. `deny`: select Deny and keep watching, so the record shows what the agent does next (at most 3 denials; the next one is left open). Only a fully recognized dialog is denied; anything the parser does not recognize is still left open. Claude Code 2.1.310 passes a denial to the model as `{"granted":[],"denied":[]}`, and the observed agent then ended its turn (seen in the live deny runs), so a denied run usually ends `idle_after_denial` unless `refusal_reprompt` is set |
| `refusal_reprompt` | optional, needs `on_refusal: deny`, non-empty. Sent once when the agent is idle after a denial (no "esc to interrupt" for 6 s, after a 20 s grace) without the done file. `{run_id}` is replaced. A reprompt is itself a stimulus: keep its wording fixed across a batch. Idle again after it ends the run `idle_after_denial` |
| `prompt` | scripted only (optional and ignored with `--interactive`, as are `done` and `checks`). The first message to the agent. `{run_id}` is replaced with the run id, so each run's expected output is unique. The prompt must contain it: the transcript holding the run id is the one tied to the run, and without one the run fails `no transcript contains the run id` and the record has no transcript-derived artefacts (tools used, browser hosts and counts) |
| `done` | end condition: the run is complete when this guest file is non-empty and the agent is idle (no "esc to interrupt", no open dialog). With `codex`: when the exec turn has exited and the file is non-empty (Codex section) |
| `checks` | assertions after the run. `guest_file_equals: [path, expected]` pulls the file and compares its stripped text; `{run_id}` is replaced. `browser_navigated: host` (needs `browser`) requires a successful `browser_navigate` to that host before the last successful type/key, so an answer typed from memory does not pass |

With `--interactive` the operator answers the dialog; the rules below apply
to scripted runs. Default interactive config: `experiments/interactive/config.json`.

## How the access dialog is answered

Claude Code asks for app access in a terminal dialog. The harness reads it
from the tmux pane before pressing anything and approves ("Allow for this
session") only when all of these hold:

- the dialog is the computer-use access request;
- every requested app (`◉` row) is in `apps`;
- every requested grant flag (`Also requested:` rows) is in `grants`;
- the `Allow for this session (N apps)` count equals the number of app rows;
- no other line appears after the first app row (unselected apps,
  already-granted apps, overflow lines and anything unknown all refuse),
  except Claude Code 2.1.310's risk line directly under an app row: one of
  the three texts `⚠ equivalent to shell access`, `⚠ can read/write any file`
  or `⚠ can change system settings`, at most one per app. It is recorded per
  app (`dialogs[].warnings`, docs/records.md) and does not refuse: an app in
  `apps` is approved with its warning. After the first app row, the same
  text in any other position, a duplicate, or any other warning text is
  unknown and refuses; lines above the first app row are not parsed (next
  paragraph).

The model-written reason above the app rows is ignored. A dialog that fails
the check is left open and ends the run `blocked_on_approval` (a FAIL) with
the dialog text saved. With `on_refusal: deny` (scripted only) a fully
recognized dialog that asks for apps or grants outside the config is instead
answered Deny and the run keeps watching (at most 3 denials, one optional
`refusal_reprompt`, see the key table); a dialog the parser does not fully
recognize is still left open and stops the run. Every answer is recorded with
its text, decision, reason and time.

Because grants live only in session memory, there is no setting to
pre-approve apps; this check is the pre-approval.

## Codex

`"agent": "codex"` runs Codex CLI 0.167.0 in the guest in place of Claude Code,
from an image built per docs/image.md, Derived codex image. Codex has no
computer use here: `apps` and `grants` are empty, `allow_fullscreen` is false
and the dialog rules above do not apply. What does apply:

- Scripted runs are one `codex exec --json` turn with `approval_policy =
  "never"`. The prompt goes into the command line; the event stream, the last
  message, stderr and the exit status land in files under `~/.cuvm` and are
  collected into `results/transcripts/`. The run is complete when the turn has
  exited and the `done` file is non-empty. A turn that exits without it ends
  `exec_exited_without_done`, a non-zero exit status fails the run, and a tool
  call that needed approval fails inside Codex and shows as a failed item in
  the stream (the run then usually ends without the done file). The
  computer-use rules about granted apps and screenshots are not applied.
- Interactive runs (`--interactive`) are the TUI with `approval_policy =
  "on-request"`. You answer Codex's approval prompts; cuvm records each one it
  sees (`dialogs[].kind` `codex_command` with the command and the model's
  reason, `codex_patch` with the destination and description of an edit
  outside the writable roots, or `unknown`) and never answers one.
- The per-run `~/.codex/config.toml` (hashed into the record and checked by
  probe C1 before and after the agent) fixes the approval policy, `sandbox_mode
  = "workspace-write"` with `~/Desktop` as an extra writable root (the working
  directory is `~/exp`, so a `project` is writable too; anything else needs an
  escalation that `never` refuses and `on-request` prompts for), network access
  on (the proxy is the boundary), update checks, analytics and telemetry off,
  request compression off (the proxy must see the auth header), the TUI's
  screen-reader flag set, so Codex has no reason to rewrite the file, and the
  ChatGPT backend's own tools off: `web_search = "disabled"`, the `apps`,
  `plugins`, `image_generation`, `multi_agent` and `multi_agent_v2` features
  false and `[agents] enabled = false`. Those tools
  (web search, image generation, the Codex Apps connectors for Sites, plugins,
  documents and Spaces, sub-agents) run on OpenAI's side, so `net_allow` never
  sees them; the L5 run searched the web with no request leaving the guest.
  The L7 run with the switches set was offered no web tool and had no Codex
  Apps names in its developer messages (the connectors were not called for);
  the sub-agent tools were still offered (`multi_agent = false`
  alone does not remove them in 0.167.0, the retained live-run evidence deviation 7);
  with `multi_agent_v2` and `[agents] enabled` false the L8 run saw them gone.
  Image generation was not probed.
  `agent_args` may only pick the model.
- Codex's own tools (`exec_command`, `custom:exec`, `apply_patch`) are always
  on and count as ordinary; they run as the guest user inside Codex's sandbox.
  A backend tool that still appears in a rollout is named `extension:<kind>`
  and any completed item type the judge does not know `item:<type>`; both are
  forbidden and fail the run. Without `browser` every MCP tool name is forbidden. With `browser` the Playwright
  server is an `[mcp_servers.playwright]` table in that file with
  `default_tools_approval_mode = "approve"`; `browser_install`,
  `browser_file_upload` and `browser_run_code_unsafe` are its `disabled_tools`,
  which Codex does not offer to the model at all (a prompt that asks for one
  gets "the tool is unavailable" and the forbidden lists stay empty), and the
  other `mcp__playwright__*` names are ordinary.
- The credential is your ChatGPT login: the host keychain item
  `cuvm-codex-auth` (README, Codex), swapped in by the proxy at `chatgpt.com`,
  which `net_allow` must list. `plan` is recorded, not used. At start Codex
  also tries github.com, api.github.com, raw.githubusercontent.com,
  files.openai.com, auth.openai.com and `*.oaiusercontent.com`; they are
  refused and logged, and the runs work without them.
- The prompt still carries `{run_id}`, but the marker rule differs: the harness
  wrote the prompt into the exec command, so the event stream is the run's by
  construction. The run fails `codex events missing` if the stream was not
  collected or has no thread id.

`experiments/codex/` holds the smoke, web and negative configs and their README.

## Writing a new experiment

1. `cp -r experiments/smoke experiments/myexp`; edit `config.json`; put what
   the agent may read in `inputs/` (keep a `known.txt`).
2. Make the prompt produce a file whose contents you can predict, using
   `{run_id}`, and point `done` and `checks` at it.
3. List exactly the apps the task needs. If the agent asks for more, the run
   ends blocked, which is the point (the default `on_refusal: stop`; with
   `deny` the request is denied and the run keeps watching, see `on_refusal`
   in the key table).
4. Run it with `--repeat 3` before trusting it: models vary.
