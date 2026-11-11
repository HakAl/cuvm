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
| browser (only with `browser`) | Playwright MCP server in the guest, started by Claude Code from the image's pinned entry point, `--isolated` (no profile on disk), `--no-webmcp`, `--proxy-server` set to the run's proxy. The guest has no other route out, so a browser that ignored the flag would reach nothing. The per-run CA is not trusted by the browser, so the swap host's TLS fails there |
| dev server (only with `dev_server`) | a guest process the harness starts from the unpacked `project` snapshot before the agent, in its own tmux server, listening on guest loopback only (probe D1 fails the run otherwise). The browser bypasses the proxy for `localhost` and `127.0.0.1` and nothing else (probe D2 checks no loopback request reached the proxy). Loopback in the guest is the guest's own `lo0`: no route to the host is added and the gateway still answers no DNS |

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
interactive (--interactive, instead of experiment) the operator's terminal is attached to the
           pane; dialogs are recorded, answered by the operator; ends when Claude exits, the
           operator ends a detached session, or deadline_s passes
experiment prompt delivered; the pane is watched: access dialogs are answered by policy,
           anything else ends the run; done when the done file exists and the agent is idle
collect    pane, transcript, done and check files, the project snapshot diff, the
           home listing diff and its changed files, results directory
teardown   always: tmux killed, login removed and E2 rechecked, probes repeated, CA
           certificate removed, VM stopped and deleted, proxy stopped
leaks      clone gone, no tart or proxy process, inputs and golden image unchanged
record     record.json and one line in runs.jsonl
```

When an error or an interrupt ends the run after the agent was ready and
before every output collector completed, the collectors that did not complete
run once as a best-effort salvage before teardown (`salvage: collected` in the
record, the original failure kept, collection failures added as fail reasons;
`salvage: guest_unreachable` with a fail reason naming the collectors not
run when the guest no longer answers, `salvage: error` when the salvage
itself failed). A collector that times out is a fail reason; the guest is
then pinged, before the next collector or after the last one, and a guest that
no longer answers skips the rest with that same fail reason and `salvage:
guest_unreachable`, in normal collection as in the salvage, with no `t_collect`
and no second attempt, so a wedged guest costs one timeout rather than every
one in turn. After the last collector a failed ping only stops the scripted
check-file pulls (every collector ran, so `t_collect` is marked).
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
connection, enforced by the proxy: the request body is exactly its
`Content-Length` (no body without one), a request with a transfer coding or an
empty, repeated or malformed length is refused, and any byte after the body
ends the exchange (before upstream if it came with the head, otherwise with
outcome `surplus_request_bytes`). A request that asks for a protocol upgrade is
answered `426 Upgrade Required` by the proxy and never forwarded (outcome
`upgrade_refused`); Codex opens a Responses WebSocket first and takes the 426 as
the signal to use HTTPS for the rest of the process. It swaps only an exact credential: an `Authorization` header that
is `Bearer <dummy>` or an `x-api-key` header that is the dummy, one per request.
The dummy anywhere else (request line, another header, extra text in the auth
header, a second auth header, or the body, scanned across reads) gets the
request refused, never rewritten, so the real token never reaches anything the
API might echo. Request heads with a bare CR or LF, or whitespace in a header
name, are refused too, so one header cannot smuggle others.
Upstream gets `Accept-Encoding: identity`, and responses are scanned for the
real token as a client would decode them: the head, then the body after
chunked framing is removed, across reads and chunk boundaries, holding back
the last bytes so the connection is closed before any part of the token
reaches the guest. Chunked bodies are re-chunked for the guest and trailers
are dropped. A response with a content coding other than identity or a
transfer coding other than chunked is cut, because the scan cannot see
through it (outcome `bad_response`, a 502 if no head was sent yet). Response
heads get the same line checks as request heads (no bare CR or LF, folded
lines or odd header names), so no coding can hide from the scan; a repeated
`Transfer-Encoding`, a 101 and a response cut off before its head or last
chunk are `bad_response` too. This is a backstop: no response has been seen to carry the token. Upstream TLS is verified against the
host's default trust store. The proxy log (`egress.jsonl`) records host,
method, path without query, which headers were swapped, status and refusals,
never either token. The real token was seen only in the Authorization header
to `api.anthropic.com` (experiments/token-isolation/findings.md).

With `agent: codex` the shape is the same with a second credential. The host
keychain item `cuvm-codex-auth` holds the operator's Codex login (`auth.json`
minus its refresh token) as JSON; the proxy reads `tokens.access_token` from
it and publishes `tokens.account_id` to the run, an identifier, not a secret.
The run writes the guest's `~/.codex/auth.json` over ssh stdin with that
account id, a per-run dummy access token (a JWT-shaped string with the standard
header, `sub: cuvm-dummy`, a random nonce, under 400 characters, no account or
plan claim), a placeholder id token of the same shape and the placeholder
refresh token `cuvm-no-refresh-token` (Codex's reader requires the field; a
refresh would go to auth.openai.com, which the allowlist refuses). The swap host
is `chatgpt.com`, the swap is the `Authorization: Bearer <dummy>` header only,
and the proxy redacts JWT-shaped strings from its log as it does `sk-ant-`
tokens. E3 compares the sha256 of the stored access token with the dummy's. The
codex E2 checks that `auth.json` is gone, that no JWT with a payload of 400 or
more characters lies under `~/.codex`, `~/exp`, `~/.zsh*` or `/etc/zshenv` (the
real token's payload is over 1000 characters, the dummy and the placeholder are
under 400 together: payload length is the on-disk discriminator, since Codex
can write its token to more places than one keychain item; a grep error such
as an unreadable file fails the check rather than passing as a clean scan), and
that `OPENAI_API_KEY` and `CODEX_API_KEY` are unset. Codex's native binary trusts
the proxy CA through `CODEX_CA_CERTIFICATE`. Codex's approval prompt is Codex's
own control: scripted runs set `approval_policy = "never"` and interactive runs
record what the operator answered; cuvm never answers it.

Threat boundary, precisely: no file, process or memory in the guest holds the
real token, so nothing the agent does can carry it out of the run. Not
guaranteed: while the run lasts, any guest process can make API calls on the
account through the proxy, the same capability `claude` has; the proxy does
not restrict API paths. Removing shell and file tools (`--tools ToolSearch`,
the settings deny list and `--disallowedTools`) narrows what the agent can do
in the guest, but an approved GUI app such as TextEdit still writes files as
the same user. With `tools` (docs/config.md) the agent has a shell or file
tools and writes files and runs processes as that user outright, including its
own `~/.claude/settings.json`, so the deny list for the tools not configured
is a default it could change, not a boundary. The host-side transcript judge
fails the run if a tool outside the config appears in the transcript, but the
transcript is written in the guest by that same agent, so the judge is evidence
it could have altered, not enforcement (Known limits). The controls are the
`--tools` flag and the installed settings file; the config refuses arguments
that would add or drop a settings source (docs/config.md `agent_args`).

What holds regardless: the VM, network and host-file boundaries above do not
depend on the agent's tools, and the clone is deleted after the run (unless
`--keep-on-fail` keeps it, or cleanup fails and is recorded).

## Approval

Claude Code's access dialog and full-screen card are its safety controls.
Grants live only in session memory, so approval is decided once, in the
config, where it can be reviewed and is hashed into the record. The harness
answers the dialog only within the config's apps and grants, refuses anything
it does not recognize, and records every answer (docs/config.md). With
`agent: codex` the equivalent control is Codex's sandbox and approval prompt,
fixed by the per-run `config.toml`; cuvm records the prompts and answers none.

## Concurrency

Nothing is shared between runs except `runs.jsonl` (appends are locked) and
`known_hosts` (all clones of one image have the same host key). Each run has
its own clone, proxy port and record directory. Two concurrent runs were
observed on separate vmnet subnets, each proxy seeing only its own guest.

## Known limits

- Output capture covers regular files (`find -type f`) under the guest home
  outside a fixed set of exclusions, in the project snapshot and in the home
  capture. Not captured, and lost with the clone without any failure recorded:
  a symlink or an empty directory the agent creates; a `.DS_Store` file;
  anything under `~/Library` (application state, caches, the keychain),
  `~/.npm`, `~/.cache`, `~/.Trash`, any file or directory named
  `node_modules` (dependency trees, caches and discards would swamp the
  archive), `~/.claude`, `~/.claude.json`, `~/.claude.json.backup` and
  `~/.local/state/claude` (Claude Code state; the transcripts are collected on
  their own), `~/Pictures/Photos Library.photoslibrary` (the Photos daemons
  rewrite its caches during a run), `~/.cuvm` (the harness); anything outside the home such as the system temporary directory
  or `/Users/Shared`. The results directory and `~/exp/project` with
  `project` are collected by their own collectors, whose failures are fail
  reasons. The home archive is capped at 512 MiB (the lists stay complete and
  the cap is a fail reason). An agent with a shell can also change the home
  after the fact, so the capture is evidence of what was there at collection,
  not of an adversary. The exclusions are the `project.HOME_PRUNES` tuple plus
  the `node_modules` term in `project.home_prunes` and the `.DS_Store` term in
  `project.HOME_LIST_CMD`, if the operator wants them narrower.
- Guest to host ports: macOS's weak host model lets the guest open TCP
  connections to host services listening on the gateway or on all
  interfaces. Probe N3 measures and records them; nothing blocks them (a `pf`
  rule would need root).
- The allowlisted hosts are full Claude endpoints: whatever your account can do
  there, the agent can do.
- With `browser`, the agent reads and acts on any page reachable through
  `net_allow`, including running page JavaScript. Page content is untrusted
  input to the model; the record shows it, nothing prevents it.
- With `browser`, Playwright MCP 0.0.83 refuses `file://` navigation by
  default (observed: `Access to "file:" protocol is blocked`). The boundary does
  not depend on it: guest files are already readable by an approved TextEdit,
  and no real credential is on the guest disk or in its keychain, only the
  run's dummy. Accepted by the operator 2026-10-26.
- With `dev_server` and external hosts in `net_allow`, a page from an external
  host can fetch `http://localhost:<port>`: loopback is a potentially
  trustworthy origin, so mixed-content blocking does not apply. Inside the
  guest that reaches only the dev server; it is recorded, not prevented.
- The browser's loopback bypass is required, not optional: observed 2026-11-07
  (web-dev design step 4a, observed in the web-dev live runs), without `--proxy-bypass`
  WebKit sent the dev-server requests to the proxy as plain HTTP, the proxy
  refused them (`not CONNECT`), the page's script and form requests failed
  with the proxy's 403, and D2 failed the run with two loopback refusals in
  the session window. The agent read the 403s as a broken dev server and
  ended its turn; the run then idled to its deadline, since scripted mode
  detects idleness only with `on_refusal: deny`.
- The full-screen card is always refused; its text has not been captured.
- A second access dialog in one session that lists already-granted apps is
  refused (the `✔` row is not accepted yet).
- Whether the final screenshot shows the task is left to a person
  (`screenshot_shows_task` is null).
- Tested on one host and one Claude Code version. The dialog grammar and the
  settings keys are version-specific.
- With `tools`, the transcript files under `~/.claude/projects` are writable
  by the agent before collection in both modes. A scripted run still fails
  without the run id in a transcript, but the id is matched as a substring and
  an edited file that keeps it is judged as is; an interactive run has no marker
  check. The forbidden-tool verdict and the transcript-derived counts are
  evidence only while the agent left the files alone; they are not a backstop
  for the tool flags and the settings file.
- With `tools`, the run verifies only `~/.claude/settings.json` in the clone. A
  managed settings file would take precedence and is not checked for (the image
  has none). `agent_args` is then an allowlist (`--model`, `--effort`,
  `--fallback-model`, one value each): Claude Code has many arguments that
  add a settings source or otherwise narrow the tools (`--settings`,
  `--managed-settings`, `--setting-sources`, `--restricted`,
  `--project-config-root`, `--permission-mode plan`, `--agents` with
  `--agent`, `--plugin-dir` hooks), and a name-based denylist missed some in
  three review rounds, so nothing else reaches `claude` from the config.
- With `agent: codex`: Codex CLI 0.167.0 only; `check_image` refuses another
  version and the image is rebuilt to move. The judge reads Codex's own event
  stream and rollouts, written in the guest by the agent's process, so as with
  `tools` it is evidence, not enforcement; Codex's sandbox and approval policy
  are the controls, and `approval_policy = "never"` with `workspace-write`
  leaves the agent a shell over `~/exp` and `~/Desktop` as the guest user.
  With a ChatGPT login the backend offers tools that run on OpenAI's side (web
  search, image generation, the Codex Apps connectors, sub-agents); they never
  cross the guest's network, so the proxy cannot see them. The per-run config
  turns them off (`web_search = "disabled"`, the `apps`, `plugins`,
  `image_generation`, `multi_agent` and `multi_agent_v2` features false,
  `[agents] enabled = false`), and the judge names
  any that still appear (`extension:<kind>`, `item:<type>`) as forbidden. The
  L7 run with the switches set found no web tool and no Codex Apps names, but
  the sub-agent tools stayed on (`multi_agent` alone does not remove them,
  the retained live-run evidence deviation 7); the L8 run with `multi_agent_v2` and `[agents]
  enabled` false saw them gone and the turn's multi-agent version disabled. The judge's
  names are evidence after the fact over completed items, not enforcement,
  and a managed setting above the user file could reinstate a server-side
  tool (not observed). The
  rollout reader was written from one probe capture (`CommandExecution` items
  and `custom_tool_call` names), the scripted L3 web run (`McpToolCall` items,
  tests/fixtures/codex/rollout-web-items.jsonl) and the interactive L5 run
  (`FileChange`, `Extension`, the `token_count` payload,
  tests/fixtures/codex/rollout-l5-items.jsonl).
  The first TUI start leaves Codex's app-server daemon running inside the
  clone until the clone is deleted. The access token in the keychain item
  lives about ten days and is refreshed by the host `codex`, not by cuvm.
  Rollouts carry the operator's account and user ids, and the agent can read
  its account id in `auth.json` and repeat it: `codex_last_message` and
  `error_events` are stored unredacted in record.json, and the stderr tail of a
  non-zero exit also enters `fail_reasons` and run.log, so every run artefact
  is as private as the rollouts beside it. The Codex path is
  verified by unit tests against captures from the probe session and the
  scripted L3 run, and by the live runs recorded in the retained live-run evidence: the
  derived image build (L0), the smoke runs (L1, L1b, L1c), the web run (L3), the
  negative run (L4), the interactive run (L5, the approval prompt kinds, the
  `token_count` payload and the backend's web search) and the negative web run
  (L7: no web tool offered with the switches set, the Codex Apps names gone,
  the sub-agent tools still offered) and its rerun with the two sub-agent
  switches (L8: the sub-agent tools gone). Still unobserved: an
  image-generation request and a run after the access token expires (L6).
