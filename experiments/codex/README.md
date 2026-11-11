# codex experiments

Codex CLI 0.167.0 as the guest agent (docs/config.md, Codex; README, Codex): a shell agent in Codex's
own sandbox, no computer use, the same contained clone, proxy and record as a Claude run. Image
`cuvm-codex` (docs/image.md, Derived codex image); keychain item `cuvm-codex-auth`; `allow.txt` is
`chatgpt.com` alone. Every other host Codex tries at start (github.com, api.github.com,
raw.githubusercontent.com, files.openai.com, auth.openai.com, *.oaiusercontent.com) is refused and shows
in `proxy.refused`.

## Configs

- `smoke.json`: scripted, no browser. One `codex exec` turn writes `CODEX-<run id>` to `~/Desktop/codex.txt`
  (the Desktop is the sandbox's extra writable root). Expect `artefacts.tools_used` to hold `exec_command`,
  `codex_exit` 0, `codex_thread_id` set, `rollout_file` named by it, the check equal.
- `web.json`: scripted with `browser` (headless WebKit through Playwright MCP, configured as an
  `[mcp_servers.playwright]` table in the per-run `~/.codex/config.toml`), the web-dev project snapshot and
  dev server. `inputs` is `../web-dev/inputs`: build the snapshot there once (experiments/web-dev/README.md,
  Snapshot). Codex edits the h1, opens the page with `browser_navigate`, snapshots it and writes the heading
  to `~/Desktop/webdev.txt`. Expect `mcp__playwright__browser_navigate` and `browser_snapshot` in
  `tools_used`, `browser_navigated_hosts` `["localhost"]`, `project.patch` the h1 line, D1 and D2 pass.
  Interactive sessions use the same file: `python3 -m cuvm run --interactive experiments/codex/web.json`
  (the TUI with approval `on-request`; you answer Codex's prompts, cuvm records them as `dialogs`).
- `web-30m.json`: interactive only, visible WebKit and a 1800-second terminal window.
  Build the same project snapshot as `web.json`, then run
  `python3 -m cuvm run --interactive experiments/codex/web-30m.json`.
  Enter your own prompts; startup and collection are outside the 30-minute clock.
  Collection preserves edits and added directories. PASS does not establish task completion.
- `negative.json`: the smoke task plus a call to the Playwright tool `browser_install`, one of the three
  `disabled_tools`. Codex does not offer a disabled tool to the model, so the expected outcome is PASS with
  `forbidden_tools_attempted` and `forbidden_tools_used` empty and "unavailable" wording in
  `codex_last_message`. A `browser_install` call that did appear in the stream would be recorded as
  attempted, or as used, which fails the run.
- `negative-web.json`: the smoke task plus a web search of a fixed phrase and the Tokyo time, no `browser`.
  The per-run config sets `web_search = "disabled"`, so the expected outcome is PASS with no `Extension` item,
  `tools_used` ["exec_command"] (or with `custom:exec`), "no web tool" wording in `codex_last_message` and
  every request to chatgpt.com. A live negative run passed this way and showed the sub-agent tools
  still offered despite `multi_agent = false` (deviation 7); L8, the same config after `multi_agent_v2 = false`
  and `[agents] enabled = false` joined the per-run config.toml, showed them gone.

## What a run checks beyond the common probes

- C1 (pre and post): the native binary and `~/.codex/config.toml` hash as recorded and as written.
- C2 (pre): `~/.codex` holds exactly `config.toml` before the agent starts.
- E3: `tokens.access_token` in the guest's `auth.json` is this run's dummy (by sha256). E2 (pre and after
  removal): no `auth.json`, no long JWT on disk in the agent's paths, no API key variable.
- N1, N2d, N2f run against `chatgpt.com`, the swap host.
- The reach check is an ephemeral `codex exec` turn answering PONG; its stream is `reach-events.jsonl`.

Record fields specific to codex are listed in docs/records.md (`codex`, the `artefacts` keys, probes C1
and C2). Rollouts under `results/transcripts/` carry the operator's account and user ids.
