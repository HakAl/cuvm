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
    results/
      transcripts/<session>.jsonl   the agent's Claude Code transcript
      screenshot-final.jpg          the last screenshot the agent took
      checks/<i>-<file>             files pulled for checks
      results.tgz                   the guest results directory
      <done file>                   the config's done file, pulled (smoke.txt for the smoke run)
```

`run_id` is `<experiment>-<UTC stamp>-<4 hex>`; the VM is `cuvm-<run_id>`.

## Verdict

PASS only if `fail_reasons` is empty. Any of these adds a reason:

- an exception in any phase (`end_reason: error`), interruption
  (`operator_interrupt`), the deadline (`deadline`) or a refused dialog
  (`blocked_on_approval`);
- any containment probe failing, before or after the agent ran;
- a forbidden tool that ran (attempts Claude Code refused are recorded in
  `forbidden_tools_attempted` and logged, but do not fail the run);
- granted apps empty or outside `apps`; no successful type or key action before
  the final screenshot; the final screenshot missing or from an error;
- a check whose file text differs;
- the proxy forwarding a host outside `net_allow`, or logging a client other
  than this run's guest;
- login removal failing; any leak (clone still listed, `tart run` or proxy
  process alive, inputs changed, golden image hashes changed).

Exit code 0 only on PASS.

Interactive runs (`"mode": "interactive"`) have no task to check. They end
`operator_exit` (Claude exited), `operator_end` (detached and chose to end),
`deadline` (the time limit; recorded, not a FAIL) or `guest_unreachable` (ssh
to the guest kept failing; a FAIL). The access-dialog, screenshot and check
rules above do not apply; every containment, cleanup, leak and proxy rule
does, and a forbidden tool that ran still fails the run.

## record.json fields

| field | contents |
| --- | --- |
| `run_id`, `mode`, `started_at`, `finished_at`, `verdict`, `fail_reasons`, `end_reason` | outcome; `mode` is `scripted` or `interactive` |
| `config`, `config_sha256`, `image`, `image_source` | what ran |
| `host` | user, uid, host name, en0 address, wildcard listeners, tart and openssl versions |
| `net` | guest IP, gateway, `proxy: egress`, proxy port and pid, allowlist |
| `guest` | ssh host key fingerprint and `host_key_pin` (`pinned`: this run recorded it into the image record; `matched`: it equalled the pin), settings.json sha256, whether `/mcp` listed computer-use, `ca_removed` |
| `golden_sha256_before`, `inputs_sha256_before` | hashes before the run |
| `probes.pre`, `probes.post`, `probes.session` | every probe by name (`session` holds E3, run right after the login is injected): `exit`, `out`, `pass`, `why`. Read `pass`, not `exit` (a refused connection exiting 1 is a pass). F4 has one entry per path (`F4:<path>`), N3 one per port (`N3:<ip>:<port>`, with `open`) |
| `auth` | login method (`dummy-login-host-swap`), plan, `attempted_at`, `injected_at`, `removed_at` |
| `reach_check` | `claude -p` PONG through the proxy before the prompt |
| `dialogs` | every dialog (interactive: approval dialogs seen by a 2 s pane sample, not menus): time, kind, apps, grants, extra lines, approved, why, full text. Interactive: `decided_by: operator`, `approved: null` (what was granted is in `artefacts.request_access_granted`), `first_seen` and `last_seen` |
| `session_stops` | interactive: each attempt to end the guest session (operator `e` or the deadline): `at`, `rc` (0 means it ran), `stderr` or `error` when it failed. A failed stop is logged; teardown then stops the VM |
| `artefacts` | interactive: `transcript_files` (all of them), tools used, forbidden tools attempted and used, `request_access_granted`, `granted_outside_config`, `pane_log`. Scripted: transcript file, tools used, forbidden tools attempted and used, granted apps, input-before-final-screenshot, final screenshot hash and file, check results, `screenshot_shows_task` (always null: a person looks) |
| `proxy` | from `egress.jsonl`: hosts `allowed` (answered requests and tunnels), `refused`, `unauthorized` clients, `requests`, `swapped` (requests whose auth header carried the dummy), `statuses`, `tunnels`, `dummy_elsewhere`, `real_in_response`, `bad_response`, `surplus_request_bytes`, `errors`, `unparsed_lines`. Each request line in `egress.jsonl` has an `outcome`: `ok`, `dummy_elsewhere`, `real_in_response`, `idle_timeout`, `connection_lost` (either side went away mid-exchange, with `lost` naming the error), `bad_response` (a response the proxy cut because it could not scan it: a content coding other than identity, a transfer coding other than chunked or a repeated one, bad chunk framing, a bare CR or LF or malformed header line in the head, a bad or 101 status line, an oversized head, or a response cut off before its head or last chunk; `why` says which) or `surplus_request_bytes` (the guest sent bytes past the first request's body; the connection was closed without forwarding them). `refused` normally includes the probe targets and the agent's own telemetry and update checks. `allowed` outside `net_allow`, any `unauthorized` client or any `real_in_response` fails a run; `dummy_elsewhere` (refused requests carrying the dummy outside the auth header) is logged, as are `bad_response` and `surplus_request_bytes` |
| `leaks` | the leak checks above with observed values; a value is `null` when it could not be observed (the reason is in `errors`), which fails the run |
| `timings`, `phases` | seconds since start at each phase, and phase timestamps |

Records identify the host: user name, uid, host name, LAN address and the
repository path appear in `host`, in F4 probe names and in `runs.jsonl`. Review
them before sharing.

## Probes

Run over ssh in the guest before the agent starts and again after login
removal. Output is recorded for passes and failures alike.

| probe | passes when |
| --- | --- |
| E1 | guest env has `HTTPS_PROXY` set to this run's proxy, `DISABLE_AUTOUPDATER=1`, and `NODE_EXTRA_CA_CERTS` pointing at `~/.cuvm/ca.pem` |
| E3 | (session, once) the access token in the guest's stored login has the sha256 of this run's dummy |
| E2 | no credentials file, no guest keychain login item, no `sk-ant-` string on disk in the agent's paths, no token env var |
| N1 | `curl` trusting only the run's CA reaches `api.anthropic.com` through the proxy, and the proxy log shows the upstream response |
| N2a | a non-allowlisted host (`example.com`) is refused (403 plus a proxy log line) |
| N2b-gw, N2b-lo | CONNECT to the gateway and to 127.0.0.1 is refused |
| N2c-1, N2c-2 | look-alike hosts (`claude.com.example.net`, `xclaude.com`) are refused |
| N2d | a CONNECT tunnel to an allowlisted host on another port (8443) is refused |
| N2f | plain HTTP to an allowlisted host is refused (403 plus a proxy log line) |
| N2e-udp, N2e-tcp | the host answers no DNS on the gateway |
| F1 | the inputs share is readable and `known.txt` hashes as on the host |
| F2 | writing to the inputs share fails |
| F3 | exactly one share is mounted |
| F4 | the host home, `~/.ssh`, `~/.claude` and repo paths do not exist in the guest |
| I1 | guest host name differs; host home and name appear in neither env nor mounts; no `id_*` key in guest `~/.ssh` |
| N3 | measured only: which host ports the guest can open on the gateway and the host's LAN address (macOS's weak host model makes these reachable; recorded, not blocked) |
