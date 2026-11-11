# Run records

Everything a run produces lands under the output root (default `./runs`):

```
runs/
  runs.jsonl                 one line per run (record.json without dialog text)
  known_hosts                guest host keys, kept out of ~/.ssh
  <run_id>/
    record.json              the full record
    run.log                  timestamped phases and failures
    tart-run.log  egress.jsonl  egress.stderr  network.txt
    egress-ca/                 the run's proxy CA and leaf certificates and keys (host only)
    inject.txt  remove-login.txt  (script output; never contains a token)
    pane-ready.txt  mcp-pane.txt  pane-full.txt  reach.txt
    pane.log  pane-log-start.txt  interactive terminal output and logging-setup diagnostic
    reach-events.jsonl         codex: the reach check's event stream
    pane-reprompt-1.txt        the pane right after a refusal_reprompt (only when one was sent)
    dev-server-pane.txt  project.patch  project-changed.tgz  project-manifest.json   (with project / dev_server)
    home-before.txt  home-after.txt  home-changed.tgz   (the home capture: listings before and after the agent, the changed files)
    results/
      transcripts/<session>.jsonl   the agent's Claude Code transcript
      transcripts/codex-events.jsonl  codex-last.txt  codex-stderr.txt  codex-exit.txt   scripted codex: the exec turn's stream, last message, stderr, exit status
      transcripts/rollout-*.jsonl   codex: Codex's session rollouts (interactive: history.jsonl too)
      screenshot-final.jpg          the last screenshot the agent took
      checks/<i>-<file>             files pulled for checks
      results.tgz                   the guest results directory (holds dev-server.log with dev_server)
      <done file>                   the config's done file, pulled (smoke.txt for the smoke run)
```

`run_id` is `<experiment>-<UTC stamp>-<4 hex>`; the VM is `cuvm-<run_id>`.

## Verdict

PASS only if `fail_reasons` is empty. Any of these adds a reason:

- an exception in any phase (`end_reason: error`), interruption
  (`operator_interrupt`), the deadline (`deadline`), a dialog left open
  (`blocked_on_approval`) or, with `on_refusal: deny`, the agent idle after a
  denial without finishing (`idle_after_denial`);
- any containment probe failing, before or after the agent ran;
- a forbidden tool that ran (attempts Claude Code refused are recorded in
  `forbidden_tools_attempted` and logged, but do not fail the run);
- granted apps empty or outside `apps`; no successful type or key action before
  the final screenshot; the final screenshot missing or from an error;
- with `agent: codex`: the exec turn exiting without the done file
  (`exec_exited_without_done`), a non-zero exit status (`codex exit <n>`), a
  missing one (`codex exit status missing`, unless the deadline is already the
  reason), or the event stream missing or without a thread id (`codex events
  missing`); the granted-apps and screenshot rules above are not applied;
- a check whose file text differs;
- the proxy forwarding a host outside `net_allow`, or logging a client other
  than this run's guest;
- login removal failing; any leak (clone still listed, `tart run` or proxy
  process alive, inputs changed, golden image hashes changed).

Exit code 0 only on PASS.

Interactive runs (`"mode": "interactive"`) have no task to check. They end
`operator_exit` (the agent exited after a successful terminal attachment),
`operator_end` (detached and chose to end), `attach_failed` (the terminal
attachment returned an error and was not recovered by reattaching; a FAIL),
`deadline` (the time limit; recorded, not a FAIL) or `guest_unreachable` (ssh
to the guest kept failing; a FAIL). The access-dialog, screenshot and check
rules above do not apply; every containment, cleanup, leak and proxy rule
does, and a forbidden tool that ran still fails the run.

## record.json fields

| field | contents |
| --- | --- |
| `run_id`, `mode`, `started_at`, `finished_at`, `verdict`, `fail_reasons`, `end_reason` | outcome; `mode` is `scripted` or `interactive` |
| `config`, `config_sha256`, `image`, `image_source` | what ran |
| `agent` | `claude` or `codex` |
| `host` | user, uid, host name, en0 address, wildcard listeners, tart and openssl versions |
| `net` | guest IP, gateway, `proxy: egress`, proxy port and pid, allowlist |
| `guest` | ssh host key fingerprint and `host_key_pin` (`pinned`: this run recorded it into the image record; `matched`: it equalled the pin), `settings_sha256` (the guest's `~/.claude/settings.json`: the image's file, or with `tools` the per-run file the run installed, null if a write failed before it was read back), with `tools` also `settings_baseline_sha256` (the image's file), whether `/mcp` listed computer-use (`mcp_playwright` is null with codex: no `/mcp` grammar is captured and `artefacts.tools_used` is the evidence), `ca_removed` |
| `tools` | with `tools` only: the configured built-in tools, recorded before the per-run settings are installed |
| `golden_sha256_before`, `inputs_sha256_before` | hashes before the run |
| `probes.pre`, `probes.post`, `probes.session` | every probe by name (`session` holds E3, run right after the login is injected): `exit`, `out`, `pass`, `why`. Read `pass`, not `exit` (a refused connection exiting 1 is a pass). F4 has one entry per path (`F4:<path>`), N3 one per port (`N3:<ip>:<port>`, with `open`); C1 and C2 with codex |
| `auth` | login method (`dummy-login-host-swap` for claude, `dummy-auth-json-host-swap` for codex), plan, `attempted_at`, `injected_at`, `removed_at` |
| `reach_check` | `claude -p` PONG through the proxy before the prompt (codex: an ephemeral `codex exec` turn writing PONG to `~/reach.txt`; its event stream is saved as `reach-events.jsonl`) |
| `dialogs` | every dialog (interactive: approval dialogs seen by a 2 s pane sample, not menus): time, kind, apps, grants, extra lines, warnings (`{app: text}` for Claude Code's `⚠` risk line under an app row, one of the three texts in 2.1.310: `equivalent to shell access`, `can read/write any file`, `can change system settings`; any other `⚠` line stays in extra), approved, why, full text. Scripted: `action` is `approved`, `denied` (`on_refusal: deny`) or `left_open` (the run then ends `blocked_on_approval`; `why` says whether the dialog was refused, an option could not be selected, or the dialog stayed open after Enter). Interactive: `decided_by: operator`, `approved: null` (what was granted is in `artefacts.request_access_granted`), `first_seen` and `last_seen`. With codex (interactive) the entries are Codex's approval prompts: kind `codex_command` with `extra` `[command, reason]` (a shell command outside the writable roots), `codex_patch` with `extra` `[destination, description]` (an apply_patch edit outside them), or `unknown` with the lines above the prompt's footer; `decided_by: operator`. Playwright calls are auto-approved by the config and raise no prompt |
| `reprompts` | scripted: `{at, text}` for the `refusal_reprompt` if it was sent (zero or one); the pane right after is saved as `pane-reprompt-1.txt` |
| `session_stops` | interactive: each attempt to end the guest session (operator `e` or the deadline), and in both modes the salvage's attempt: `at`, `rc` (0 means it ran), `stderr` or `error` when it failed. A failed stop is logged; teardown then stops the VM |
| `artefacts` | interactive: `transcript_files` (all of them), tools used, forbidden tools attempted and used, `request_access_granted`, `granted_outside_config`, `pane_log`. Scripted: transcript file, tools used, forbidden tools attempted and used, granted apps, input-before-final-screenshot, final screenshot hash and file, check results, `screenshot_shows_task` (always null: a person looks). With codex, scripted: `events_file`, `last_message_file`, `stderr_file`, `exit_file` (under `results/transcripts/`, null when the exec turn did not write one), `rollout_files` and `rollout_file` (the rollout named by the thread id; null with a log line when none matches), `tools_used` (`exec_command`, `custom:exec`, `apply_patch`, `mcp__<server>__<tool>`; `extension:<kind>` for a tool the ChatGPT backend ran on its side, such as `extension:web.search`, and `item:<type>` for any completed item type the judge does not know; both are forbidden), the forbidden lists, `items` (completed items by type), `unparsed_lines`, `codex_usage` (`last` turn's usage, `sum` over turns, `turns`), `codex_thread_id`, `codex_last_message` (the last agent message, 2000 characters), `error_events` (texts of `turn.failed` and `error` events; a turn can complete after `error` events, as with transport retries, so the list is not a failure by itself), `codex_exit` (the integer from `EXIT=<n>`, null when absent or malformed), `checks`; the computer-use fields keep their names with empty or null values and there is no `transcript_file`, `screenshot_shows_task` or `reprompts`. With codex, interactive: `transcript_files` are the rollouts, `codex_usage` (the last `token_count` payload of each rollout: `info.total_token_usage` summed across files, the other fields, `info.last_token_usage`, `info.model_context_window` and `rate_limits`, from the last file; `type`, `thread_id` and `turn_id` are dropped), `history_file` (`~/.codex/history.jsonl` when it exists). Rollouts carry the operator's account and user ids (`creator_account_id`, `creator_user_id` in `session_meta`): review before sharing |
| `proxy` | from `egress.jsonl`: hosts `allowed` (answered requests and tunnels), `refused`, `unauthorized` clients, `requests`, `swapped` (requests forwarded upstream with the dummy in the auth header replaced by the real credential; a request the proxy answered itself, a refused upgrade for one, logs `swapped: []` and is not counted even when it carried the dummy), `statuses`, `tunnels`, `dummy_elsewhere`, `real_in_response`, `bad_response`, `surplus_request_bytes`, `upgrade_refused`, `errors`, `unparsed_lines`. A request line's `other_token_headers` names headers other
than the auth header whose value looks like a token; with `agent: codex` it lists `cookie` on most requests, a
token-shaped cookie that chatgpt.com sets in its responses and Codex echoes back, not the credential (the dummy
and the real token are matched exactly; a leak would show as `dummy_elsewhere` or `real_in_response`). Each
request line in `egress.jsonl` has an `outcome`: `ok`, `dummy_elsewhere`, `real_in_response`, `idle_timeout`, `connection_lost` (either side went away mid-exchange, with `lost` naming the error), `bad_response` (a response the proxy cut because it could not scan it: a content coding other than identity, a transfer coding other than chunked or a repeated one, bad chunk framing, a bare CR or LF or malformed header line in the head, a bad or 101 status line, an oversized head, or a response cut off before its head or last chunk; `why` says which), `surplus_request_bytes` (the guest sent bytes past the first request's body; the connection was closed without forwarding them) or `upgrade_refused` (the request asked for a protocol upgrade, Codex's Responses WebSocket; the proxy answered 426 Upgrade Required itself and forwarded nothing; `upgrade` names the requested protocol). `refused` normally includes the probe targets and the agent's own telemetry and update checks. `allowed` outside `net_allow`, any `unauthorized` client or any `real_in_response` fails a run; `dummy_elsewhere` (refused requests carrying the dummy outside the auth header) is logged, as are `bad_response` and `surplus_request_bytes` |
| `browser` | with `browser` only: `engine`, `headless`, the image's `browser` block, the MCP config written to the guest and its sha256. Probe `B1` (pre and post) checks that config and the entry point's sha256. `guest.mcp_playwright` says whether `/mcp` showed it connected. With codex `mcp_config` is the Playwright table of `~/.codex/config.toml` and `mcp_config_sha256` that file's sha256 |
| `codex` | with `agent: codex` only: `version`, `binary`, `binary_sha256` (the image record's block), `mode`, `config_sha256` (the per-run `~/.codex/config.toml` as read back; null if the write failed). Probes C1 (pre and post) and C2 (pre) check it |
| browser fields in `artefacts` | always present: `browser_navigated_hosts` (hosts of successful `browser_navigate` calls) in both modes, and in scripted runs `browser_hosts_before_input` (those before the last successful type/key). Empty lists without `browser`. `browser_console_errors` and `browser_network_failed` count error-level lines in `browser_console_messages` results and requests with status 400 or higher or a failed outcome in `browser_network_requests` results: best effort from the tools the agent chose to call, summed across transcripts in interactive mode |
| `project` | with `project` only: `archive`, `archive_sha256`, `top_dir`, `dir`, `unpack_s`, `files`, then after collection `added`, `modified`, `deleted` (paths relative to `~/exp/project`, so they start with the top directory, `fixture/index.html`; a directory the agent creates beside the project, `screensavers/src/main.js`, is listed and archived too; `node_modules` excluded; all three null when the after-manifest could not be taken, with a fail reason), `patch` (`project.patch`, or null if it could not be made) and `changed_archive` (`project-changed.tgz`, or null when nothing was added or modified). All five fields are present as soon as collection starts, so a guest timeout at any step leaves them null rather than missing. `project-manifest.json` holds the before and after sha256 manifests (`after` null when it could not be taken) and is written before the first collection command |
| `home_changes` | always: what the agent wrote anywhere else under its home. Regular files under `~` are listed (path, mtime, ctime, size) right before `t_ready` and again at collection; `added`, `modified` (any of the three values differ; ctime moves even when a rewrite keeps the size and restores the mtime), `deleted`, paths relative to `~`. Pruned: `~/Library`, `~/.claude` (transcripts are collected on their own), `~/.claude.json` and its backup, `~/.local/state/claude`, `~/.cuvm`, `~/.npm`, `~/.cache`, `~/.Trash`, `~/Pictures/Photos Library.photoslibrary` (daemon-written), the `results` directory (results.tgz), `~/exp/project` when `project` is set (the project snapshot), every `node_modules`, `.DS_Store`; a file the agent saves under one of these is not listed and is lost with the clone without a fail reason (docs/design.md Known limits). `archive` is `home-changed.tgz` with the added and modified files (null when there are none or none fit), `files` and `bytes` count the whole added plus modified list, `capped` is true when that list exceeds 512 MiB (the archive then holds the sorted prefix that fits, the lists stay complete, and it is a fail reason), `bad_names` counts listing records refused for a control character or backslash in the name (non-zero is a fail reason; the other records are kept). The fields are present and null from the before listing on, so a timeout at any step leaves them null rather than missing; a failed or timed-out before listing sets the whole object to null with a fail reason and the run goes on. Files with extended attributes may bring an AppleDouble `._` entry into the archive (bsdtar). Not covered: symlinks, empty directories, anything outside the home |
| `dev_server` | with `dev_server` only: `command`, `port`, `cwd`, `log`, `started_at`, `ready`, `ready_at`, `listen` (the observed listener addresses) |
| `leaks` | the leak checks above with observed values; a value is `null` when it could not be observed (the reason is in `errors`), which fails the run |
| `timings`, `phases` | seconds since start at each phase, and phase timestamps |
| `salvage` | present when not every output collector (project, home, results) completed in normal collection, because an error or an interrupt ended the run after `t_ready` (in the agent phase or during collection itself) or because the guest stopped answering: `collected` when the guest session was ended and the collectors that had not completed ran before teardown (one cut short by the interrupt runs again from the top; their failures are fail reasons beside the original one, `t_collect` carries `salvage: true`), `guest_unreachable` when the guest no longer answered, at the ping after a collector timed out (in normal collection or in the salvage; the salvage is not tried again) or when the salvage started; the fail reason names the collectors not run and there is no `t_collect`; `error` when the salvage itself raised (a fail reason). Absent otherwise. In both normal collection and salvage, a collector that times out is a fail reason and the guest is pinged afterwards, before the next collector or, after the last one, before anything else is asked of it; `guest unreachable after a collection timeout` names the collectors skipped, or says `every collector ran` when the last one timed out (then `t_collect` is marked, no `salvage` field is set, and in scripted mode the `guest_file_equals` files are not pulled: `got` null) |

When a run's output archives are missing or capped, `experiments/web-dev/replay_transcript.py`
rebuilds files from the transcript as a fallback: it replays the `Write` and `Edit` tool calls, and
only the `Bash` calls whose indexes the caller has read and passed, in transcript order into a
directory on the host. It reproduces what those calls wrote, not the guest's final state: a command's
side effects, files produced by tools it did not replay, and anything done after the transcript was
written are out of its reach, and a transcript an agent with a shell could edit is not proof. The
collected archives are the evidence; the replay is a recovery aid (the first interactive web-dev run).

Records identify the host: user name, uid, host name, LAN address and the
repository path appear in `host`, in F4 probe names and in `runs.jsonl`. Review
them before sharing.

## Probes

Run over ssh in the guest before the agent starts and again after login
removal. Output is recorded for passes and failures alike.

| probe | passes when |
| --- | --- |
| E1 | guest env has `HTTPS_PROXY` set to this run's proxy, `DISABLE_AUTOUPDATER=1`, and `NODE_EXTRA_CA_CERTS` pointing at `~/.cuvm/ca.pem` |
| E3 | (session, once) the access token in the guest's stored login (claude: the keychain login item; codex: `tokens.access_token` in `~/.codex/auth.json`) has the sha256 of this run's dummy |
| E2 | claude: no credentials file, no guest keychain login item, no `sk-ant-` string on disk in the agent's paths, no token env var. codex: `~/.codex/auth.json` absent; no JWT with a payload of 400 or more characters under `~/.codex`, `~/exp` (`node_modules` excluded), `~/.zsh*` or `/etc/zshenv` (the real token's payload is over 1000 characters, the dummy and the placeholder id token are under 400 in total; a grep error such as an unreadable file is `scan incomplete`, a failure); `OPENAI_API_KEY` and `CODEX_API_KEY` unset |
| N1 | `curl` trusting only the run's CA reaches the swap host (`api.anthropic.com`; `chatgpt.com` with codex) through the proxy with a 2xx, 3xx or 4xx status, and the proxy log shows the upstream response |
| N2a | a non-allowlisted host (`example.com`) is refused (403 plus a proxy log line) |
| N2b-gw, N2b-lo | CONNECT to the gateway and to 127.0.0.1 is refused |
| N2c-1, N2c-2 | look-alike hosts (`claude.com.example.net`, `xclaude.com`) are refused |
| N2d | a CONNECT tunnel to the swap host on another port (8443) is refused |
| N2f | plain HTTP to the swap host is refused (403 plus a proxy log line) |
| N2e-udp, N2e-tcp | the host answers no DNS on the gateway |
| F1 | the inputs share is readable and `known.txt` hashes as on the host |
| F2 | writing to the inputs share fails |
| F3 | exactly one share is mounted |
| F4 | the host home, `~/.ssh`, `~/.claude`, `~/.codex` and repo paths do not exist in the guest |
| I1 | guest host name differs; host home and name appear in neither env nor mounts; no `id_*` key in guest `~/.ssh` |
| N3 | measured only: which host ports the guest can open on the gateway and the host's LAN address (macOS's weak host model makes these reachable; recorded, not blocked) |
| D1 | (pre, with `dev_server`) the dev server answered 200 on `127.0.0.1:<port>` and `lsof` shows every listener on that port on `127.0.0.1` or `[::1]`; recorded by the dev server start, a failure stops the run before the login is injected |
| D2 | (post, with `dev_server`) no `refused` entry for `localhost`, `127.0.0.1` or `::1` in the egress log between the end of the pre-probes and the start of the post-probes (the pre and post N2b-lo refusals are outside that window) |
| C1 | (codex, pre and post) sha256 of the Codex native binary equals the image record and sha256 of `~/.codex/config.toml` equals the text the run wrote |
| C2 | (codex, pre only) `~/.codex` holds exactly `config.toml`: no `auth.json`, daemon copy or session left by the image build |
