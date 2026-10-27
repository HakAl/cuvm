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
| `agent` | only `claude` is implemented |
| `agent_args` | arguments to `claude` in the guest. `--tools ToolSearch` keeps only ToolSearch from the built-in tools (the computer-use MCP tools load through it); keep it unless the experiment is about tools |
| `image` | name of the golden VM; its record must exist in `images/<image>.json` |
| `plan` | `max` or `pro`, written into the injected login; computer use needs a paid plan |
| `inputs` | host directory shared read-only into the guest at `/Volumes/My Shared Files/inputs`. Must contain `known.txt` (probe F1 hashes it) |
| `results` | guest directory the agent may write to, a path under `~/` (letters, digits, `.`, `_`, `-`); its contents are collected as `results.tgz`. The golden image has `~/exp/results`; another directory must exist in the image or be created by the agent. A failed archive or copy fails the run |
| `deadline_s` | wall-clock limit for the agent phase; the run ends `deadline` and FAILs after it. Interactive: the session's time limit (warning five minutes before; recorded, not a FAIL) |
| `net_allow` | optional; file of hostnames the proxy forwards (fnmatch patterns, one per line). Default `cuvm/data/allow-run.txt`: `api.anthropic.com`, `claude.ai`, `platform.claude.com`, `claude.com` |
| `browser` | optional; `webkit` (or `chromium` if the image has it). Starts a Playwright MCP server in the guest (`mcp__playwright__*` tools), pinned and hash-checked by the image record's `browser` block (probe B1). Its traffic uses the run's proxy and `net_allow`. `browser_install`, `browser_file_upload` and `browser_run_code_unsafe` are denied and count as forbidden tools. Needs an image built per docs/image.md, Derived browser image. Several allowed Playwright tools take a `filename` and write their output under the agent's working directory (`~/exp`) or the browser output directory (`<results>/browser`), so with `browser` keep `done` and `guest_file_equals` targets outside both |
| `browser_headless` | optional, with `browser`; default false (the browser window shows in the Tart window) |
| `apps` | apps the harness may approve in the access dialog. At least one |
| `grants` | extra grant flags the harness may approve: `clipboardRead`, `clipboardWrite`, `systemKeyCombos`. Default none |
| `allow_fullscreen` | intended to allow the full-screen takeover card. In v0.1 the card is always refused (its text has not been captured, so there is nothing to check it against) |
| `prompt` | scripted only (optional and ignored with `--interactive`, as are `done` and `checks`). The first message to the agent. `{run_id}` is replaced with the run id, so each run's expected output is unique |
| `done` | end condition: the run is complete when this guest file is non-empty and the agent is idle (no "esc to interrupt", no open dialog) |
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
  already-granted apps, warnings such as "equivalent to shell access",
  overflow lines and anything unknown all refuse).

The model-written reason above the app rows is ignored. Anything that fails
ends the run `blocked_on_approval` (a FAIL), with the dialog text saved. Every
answer is recorded with its text, decision, reason and time.

Because grants live only in session memory, there is no setting to
pre-approve apps; this check is the pre-approval.

## Writing a new experiment

1. `cp -r experiments/smoke experiments/myexp`; edit `config.json`; put what
   the agent may read in `inputs/` (keep a `known.txt`).
2. Make the prompt produce a file whose contents you can predict, using
   `{run_id}`, and point `done` and `checks` at it.
3. List exactly the apps the task needs. If the agent asks for more, the run
   ends blocked, which is the point.
4. Run it with `--repeat 3` before trusting it: models vary.
