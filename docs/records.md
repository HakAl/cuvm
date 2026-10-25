# Run records

Everything a run produces lands under the output root (default `./runs`):

```
runs/
  runs.jsonl                 one line per run (record.json without dialog text)
  known_hosts                guest host keys, kept out of ~/.ssh
  <run_id>/
    record.json              the full record
    run.log                  timestamped phases and failures
    tart-run.log  tinyproxy.conf  tinyproxy.log  network.txt
    inject.txt  remove-login.txt  (script output; never contains the token)
    pane-ready.txt  mcp-pane.txt  pane-full.txt  reach.txt
    results/
      transcripts/<session>.jsonl   the agent's Claude Code transcript
      screenshot-final.jpg          the last screenshot the agent took
      checks/<i>-<file>             files pulled for checks
      results.tgz                   the guest results directory
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

## record.json fields

| field | contents |
| --- | --- |
| `run_id`, `started_at`, `finished_at`, `verdict`, `fail_reasons`, `end_reason` | outcome |
| `config`, `config_sha256`, `image`, `image_source` | what ran |
| `host` | user, uid, host name, en0 address, wildcard listeners, tart and tinyproxy versions |
| `net` | guest IP, gateway, proxy port and pid, allowlist |
| `guest` | ssh host key fingerprint, settings.json sha256, whether `/mcp` listed computer-use |
| `golden_sha256_before`, `inputs_sha256_before` | hashes before the run |
| `probes.pre`, `probes.post` | every probe: exit code, output, pass, reason |
| `auth` | login method, plan, `attempted_at`, `injected_at`, `removed_at` |
| `reach_check` | `claude -p` PONG through the proxy before the prompt |
| `dialogs` | every dialog: time, kind, apps, grants, extra lines, approved, why, full text |
| `artefacts` | transcript file, tools used, forbidden tools attempted and used, granted apps, input-before-final-screenshot, final screenshot hash and file, check results, `screenshot_shows_task` (always null: a person looks) |
| `proxy` | hosts allowed, denied and requested, unauthorized clients |
| `leaks` | the leak checks above with observed values |
| `timings`, `phases` | seconds since start at each phase, and phase timestamps |

## Probes

Run over ssh in the guest before the agent starts and again after login
removal. Output is recorded for passes and failures alike.

| probe | passes when |
| --- | --- |
| E1 | guest env has `HTTPS_PROXY` set to this run's proxy, and `DISABLE_AUTOUPDATER=1` |
| E2 | no credentials file, no guest keychain login item, no `sk-ant-` string on disk in the agent's paths, no token env var |
| N1 | the proxy reaches `api.anthropic.com` with a valid certificate |
| N2a | a non-allowlisted host (`example.com`) is refused (403 plus a proxy log line) |
| N2b-gw, N2b-lo | CONNECT to the gateway and to 127.0.0.1 is refused |
| N2c-1, N2c-2 | look-alike hosts (`claude.com.example.net`, `xclaude.com`) are refused |
| N2d | an allowlisted host on another port (8443) is refused |
| N2e-udp, N2e-tcp | the host answers no DNS on the gateway |
| F1 | the inputs share is readable and `known.txt` hashes as on the host |
| F2 | writing to the inputs share fails |
| F3 | exactly one share is mounted |
| F4 | the host home, `~/.ssh`, `~/.claude` and repo paths do not exist in the guest |
| I1 | guest host name differs; host home and name appear in neither env nor mounts; no `id_*` key in guest `~/.ssh` |
| N3 | measured only: which host ports the guest can open on the gateway and the host's LAN address (macOS's weak host model makes these reachable; recorded, not blocked) |
