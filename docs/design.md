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
| network | `--net-host` (host-only vmnet); the only way out is the host's tinyproxy, per run, bound to the gateway, allowing only this guest and only listed hosts. CONNECT (HTTPS) is limited to port 443; plain HTTP requests to a listed host are forwarded on any port |
| clipboard, audio, USB | off (`--no-clipboard --no-audio --no-usb-accessories`) |
| guest agent | not installed |

The guest's own DNS is never answered by the host; name resolution happens in
the proxy.

## Lifecycle of one run

```
preflight  golden image hashes equal the image record; inputs hashed; host listeners noted
clone      tart clone golden cuvm-<run_id>
boot       tart run with the link flags; wait for an IP
proxy      tinyproxy on a free port at the gateway, allowlist from the config
connect    ssh up; host key equals the pinned one; guest settings baseline hash matches;
           /etc/zshenv set to this run's proxy port; router and DNS set to the gateway
probes     containment probes (docs/records.md); any failure ends the run before the agent starts
session    login injected into the guest keychain; claude started in tmux with the config's
           arguments; /mcp must list computer-use; a one-shot PONG proves API reach
experiment prompt delivered; the pane is watched: access dialogs are answered by policy,
           anything else ends the run; done when the done file exists and the agent is idle
collect    pane, transcript, done and check files, results directory
teardown   always: tmux killed, login removed and E2 rechecked, probes repeated,
           VM stopped and deleted, proxy stopped
leaks      clone gone, no tart or proxy process, inputs and golden image unchanged
record     record.json and one line in runs.jsonl
```

Teardown runs after errors and after SIGINT or SIGTERM (a second signal is
ignored until the record is written). A cleanup step that fails, or a leak
check that cannot be observed, fails the run and is recorded. `--keep-on-fail`
never keeps a clone whose login removal could not be confirmed.

Teardown cannot run if the harness is killed (SIGKILL, SIGHUP from a closed
terminal, a crash or power loss). A clone left that way may still hold the
login in its keychain. To find and remove leftovers:

```
tart list | grep ' cuvm-'                 # clones are named cuvm-<run_id>
pgrep -fl 'tart run cuvm-'; pgrep -fl tinyproxy
tart stop cuvm-<run_id>; tart delete cuvm-<run_id>   # do not boot it to look inside
pkill -f 'tart run cuvm-<run_id>'; kill <tinyproxy pid>
```

Only do this when no run is in progress. If a clone was left behind,
consider revoking the token.

## Credentials

The golden image holds no credentials. Each run builds a claude.ai login object
from a long-lived token kept in the host keychain and writes it into the
guest's login keychain over ssh stdin (never argv, a file or output). It has no
refresh token. Teardown deletes it and probe E2 checks nothing credential-like
is left.

Threat boundary, precisely: during a run the token is in the clone's keychain
(encrypted at rest with the public Tart default password) and in `claude`'s
memory. Treat the guest, and the login in it, as exposed to the agent. Removing
shell and file tools (`--tools ToolSearch`, the settings deny list and
`--disallowedTools`) takes away the direct route to `security`, but it is not
credential isolation: an approved GUI app such as TextEdit writes files as the
same user, and a saved `~/.zshenv` would run in the next shell the harness opens
over ssh. That path has not been demonstrated, and nothing in cuvm prevents
it. Likewise, probe E2 and the login removal run inside the guest, so after a
hostile session they are measurements, not proof that the token is gone.

What holds regardless: the injected login has no refresh token, the clone is
deleted after the run, and the VM, network and host-file boundaries above do
not depend on the agent's tools. If the token must be isolated from the agent,
that needs a boundary cuvm does not have yet. Use a token made for this
purpose, which you are prepared to revoke.

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
