# Design

## Goal

A safe place to try computer use and similar screen-driving features of agent
CLIs, starting with Claude Code. One command creates a session from a config,
runs an experiment inside a boundary enforced below the agent, and leaves a
record. You interact only when building the image; runs are unattended.

The harness does not define experiments. It promises a clean, measured,
disposable box and an honest record of what happened in it.

## Facts it rests on (Claude Code 2.1.310)

- Computer use is a built-in MCP server (`computer-use`) that Claude Code
  spawns from its own binary. No desktop app is involved.
- It needs an interactive session (not `-p`), a Pro or Max claude.ai login,
  macOS, and a per-project opt-in from `/mcp` that persists.
- App access is granted per session through one terminal dialog that allows
  the requested set or denies it. Clipboard read, clipboard write and system
  key combos are separate flags. Full-screen takeover has its own card.
- Screenshots exclude apps that were not granted, and the terminal. Input to
  those apps is rejected. Browsers are view-only, terminals click-only.
- None of this is scoped to a directory or a desktop Space. Only the OS can
  scope it, so the boundary is a VM.

## Boundary

The agent runs in a Tart macOS VM cloned from a golden image. Every link
between host and guest is listed, and each one is either needed or switched
off:

| link | state |
| --- | --- |
| display | the Tart window, for watching |
| file share | one read-only folder (`inputs`) |
| ssh | host to guest only, for control and collection; key auth, known_hosts in the output root |
| network | `--net-host` (host-only vmnet); the only way out is the host's egress proxy (`cuvm/egress.py`), per run, bound to the gateway, allowing only this guest and only listed hosts, and only CONNECT to port 443. Plain HTTP and anything else gets 403. `api.anthropic.com` is terminated by the proxy (per-run CA, see Credentials); other listed hosts get a plain tunnel the proxy does not inspect |
| clipboard, audio, USB | off (`--no-clipboard --no-audio --no-usb-accessories`) |
| guest agent | not installed |

The guest's own DNS is never answered by the host; name resolution happens in
the proxy.

## Lifecycle of one run

```
preflight  golden image hashes equal the image record; inputs hashed; host listeners noted
clone      tart clone golden cuvm-<run_id>
boot       tart run with the link flags; wait for an IP
proxy      cuvm/egress.py on a free port at the gateway, allowlist from the config; it reads
           the real token from the host keychain and makes a per-run CA
connect    ssh up; host key equals the pinned one; guest settings baseline hash matches;
           /etc/zshenv set to this run's proxy port; the run's CA certificate copied to
           ~/.cuvm/ca.pem and trusted via NODE_EXTRA_CA_CERTS; router and DNS set to the gateway
probes     containment probes (docs/records.md); any failure ends the run before the agent starts
session    a dummy login injected into the guest keychain, E3 checks it is this run's dummy;
           claude started in tmux with the config's
           arguments; /mcp must list computer-use; a one-shot PONG proves API reach
experiment prompt delivered; the pane is watched: access dialogs are answered by policy,
           anything else ends the run; done when the done file exists and the agent is idle
collect    pane, transcript, done and check files, results directory
teardown   always: tmux killed, login removed and E2 rechecked, probes repeated, CA
           certificate removed, VM stopped and deleted, proxy stopped
leaks      clone gone, no tart or proxy process, inputs and golden image unchanged
record     record.json and one line in runs.jsonl
```

Teardown runs after errors and after SIGINT or SIGTERM (a second signal is
ignored until the record is written). A cleanup step that fails, or a leak
check that cannot be observed, fails the run and is recorded. `--keep-on-fail`
never keeps a clone whose login removal could not be confirmed. A kept clone
can only hold the run's dummy, which works only through that run's proxy.
Delete kept clones when you are done.

Teardown cannot run if the harness is killed (SIGKILL, SIGHUP from a closed
terminal, a crash or power loss). A clone left that way may still hold the
run's dummy login (harmless without its proxy) and the run's public CA
certificate. To find and remove leftovers:

```
tart list | grep -E ' cuvm-.+-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{4} '   # run clones: cuvm-<name>-<stamp>-<hex>; never your golden image
pgrep -fl 'tart run cuvm-'; pgrep -fl 'cuvm/egress.py'
tart stop cuvm-<run_id>; tart delete cuvm-<run_id>   # do not boot it to look inside
pkill -f 'tart run cuvm-<run_id>'; kill <egress.py pid>
```

Only do this when no run is in progress.

## Credentials

The real token never enters the guest. It stays in the host keychain
(`CUVM_KEYCHAIN_ITEM`) and is read once, into memory, by the egress proxy
process; nothing else in cuvm reads it. Each run makes a dummy access token
(`sk-ant-oat01-cuvm-dummy-<random>`) and writes a claude.ai login object
holding the dummy, with no refresh token, into the guest's login keychain over
ssh stdin. Probe E3 checks the stored token is this run's dummy (by sha256).
Teardown deletes the login and probe E2 checks nothing credential-like is left.

The proxy terminates TLS for `api.anthropic.com` only, with a leaf certificate
from a per-run CA (2 days valid; the key stays in the run directory on the
host; the guest trusts the CA through `NODE_EXTRA_CA_CERTS`). One request per
connection. It swaps only an exact credential: an `Authorization` header that
is `Bearer <dummy>` or an `x-api-key` header that is the dummy, one per request.
The dummy anywhere else (request line, another header, extra text in the auth
header, a second auth header, or the body, scanned across reads) gets the
request refused, never rewritten, so the real token never reaches anything the
API might echo. Request heads with a bare CR or LF, or whitespace in a header
name, are refused too, so one header cannot smuggle others.
Upstream gets `Accept-Encoding: identity`, and responses are scanned for the
real token across reads, holding back the last bytes so the connection is
closed before any part of the token reaches the guest (a backstop: no
response has been seen to carry it). Upstream TLS is verified against the
host's default trust store. The proxy log (`egress.jsonl`) records host,
method, path without query, which headers were swapped, status and refusals,
never either token. The real token was seen only in the Authorization header
to `api.anthropic.com` (experiments/token-isolation/findings.md).

Threat boundary, precisely: no file, process or memory in the guest holds the
real token, so nothing the agent does can carry it out of the run. Not
guaranteed: while the run lasts, any guest process can make API calls on the
account through the proxy, the same capability `claude` has; the proxy does
not restrict API paths. Removing shell and file tools (`--tools ToolSearch`,
the settings deny list and `--disallowedTools`) narrows what the agent can do
in the guest, but an approved GUI app such as TextEdit still writes files as
the same user.

What holds regardless: the VM, network and host-file boundaries above do not
depend on the agent's tools, and the clone is deleted after the run (unless
`--keep-on-fail` keeps it, or cleanup fails and is recorded).

## Approval

Claude Code's access dialog and full-screen card are its safety controls.
Grants live only in session memory, so approval is decided once, in the
config, where it can be reviewed and is hashed into the record. The harness
answers the dialog only within the config's apps and grants, refuses anything
it does not recognize, and records every answer (docs/config.md).

## Concurrency

Nothing is shared between runs except `runs.jsonl` (appends are locked) and
`known_hosts` (all clones of one image have the same host key). Each run has
its own clone, proxy port and record directory. Two concurrent runs were
observed on separate vmnet subnets, each proxy seeing only its own guest.

## Known limits

- Guest to host ports: macOS's weak host model lets the guest open TCP
  connections to host services listening on the gateway or on all
  interfaces. Probe N3 measures and records them; nothing blocks them (a `pf`
  rule would need root).
- The allowlisted hosts are full Claude endpoints: whatever your account can do
  there, the agent can do.
- The full-screen card is always refused; its text has not been captured.
- A second access dialog in one session that lists already-granted apps is
  refused (the `✔` row is not accepted yet).
- Whether the final screenshot shows the task is left to a person
  (`screenshot_shows_task` is null).
- Tested on one host and one Claude Code version. The dialog grammar and the
  settings keys are version-specific.
