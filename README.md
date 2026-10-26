# cuvm

Run Claude Code computer-use experiments inside a disposable macOS VM, one
command per run, with containment checked before and after and a record of
everything that happened.

```
python3 -m cuvm run experiments/smoke/config.json
```

Each run clones a golden image you build once, boots it, starts a per-run
egress proxy, probes that the box is sealed, signs the agent in for that run
only, gives it the prompt, answers its app-access dialog against your config,
checks the result, removes the login, deletes the clone, checks nothing leaked
and appends a record. The exit code is 0 only on PASS. Two runs can execute at
once (Apple allows two macOS guests per host).

There is also an interactive mode (`run --interactive`): the same contained
clone, but your terminal is attached to the agent's session and you talk to it
yourself. See [Interactive sessions](#interactive-sessions).

One thing it does not do, so you know before you build an image:

- No web browsing by the agent. Claude Code's computer use treats browsers as
  view-only: it can see Safari in a screenshot but its clicks and typing there
  are refused. Use apps such as TextEdit, Notes or Calendar.

Status: v0.1, tested on one host (Apple silicon, macOS 26, Tart 2.40.1,
Claude Code 2.1.310 in the guest). Only the `claude` agent is
implemented.

## Why

Computer use lets a model see a screen and drive the mouse and keyboard. On
your own desktop, the boundary is your whole account. Here the boundary is a
throwaway VM that cannot see your files, can reach only the hosts you list, and
never holds your login token: the guest gets a dummy, and a proxy on the host
swaps in the real token on its way to the Anthropic API.

## Safety model, in short

| layer | what it does | how it is checked each run |
| --- | --- | --- |
| VM | Tart clone of a golden image, deleted afterwards | golden hashes unchanged, clone gone |
| filesystem | one read-only share (`inputs/`), no host paths | probes F1-F4 |
| network | only a proxy on the host (`cuvm/egress.py`), allowlisted hosts, HTTPS to port 443 only; plain HTTP refused | probes N1-N3, proxy log |
| identity | guest has its own user, host name and keys | probe I1 |
| credentials | the real token stays on the host; the guest login holds a per-run dummy that the host proxy swaps for the real token in the Authorization header to api.anthropic.com only | probe E3 (guest holds the dummy), E2 before and after, proxy log |
| agent tools | built-in tools reduced to ToolSearch; shell and file tools denied | transcript check |
| app access | the access dialog is approved only for the config's apps | dialog record |

What it does not protect: anything the allowlisted hosts can do with your
account; API calls on your account during a run (any guest process can send
requests through the proxy, which adds the real token, the same capability
`claude` itself has; the token itself never enters the guest, so it cannot be
carried out of the run); and the guest-to-host ports listed in the record
(probe N3 measures them but does not block them). Details and limits: [docs/design.md](docs/design.md).

## Requirements

- Apple silicon Mac on macOS 26 (the only host version tested), about 60 GB
  free disk on APFS.
- [Tart](https://tart.run): `brew install cirruslabs/cli/tart`.
- `openssl` on `PATH` (the macOS system LibreSSL works) for the per-run proxy certificates.
- Python 3.11 or newer (standard library only) and zsh (the macOS default shell).
- Claude Code on the host, only to run `claude setup-token` (or make the token
  on any machine and paste it at the keychain prompt).
- A Claude Pro or Max plan (computer use needs one) and a long-lived token
  from `claude setup-token`.
- The terminal app you run cuvm from needs Local Network access
  (System Settings, Privacy and Security, Local Network), or ssh to the guest
  fails with "No route to host".

## Quickstart

1. Store your token in the host keychain (interactive prompt, so the token
   stays out of shell history). Only the host proxy reads it; the image build
   and every run give the guest a dummy instead.
   ```
   claude setup-token
   security add-generic-password -a "$USER" -s cuvm-claude-oauth -w
   ```
2. Build the golden image once, by following [docs/image.md](docs/image.md)
   (about an hour, mostly downloads; one GUI session for the macOS grants).
3. Record the image (it must be stopped). If you did the last step of
   docs/image.md this is already done; running it again on an unchanged image
   keeps the recorded source and host key.
   ```
   python3 -m cuvm image record cuvm-golden
   ```
4. Run the smoke experiment. It asks the agent to type a run-specific line in
   TextEdit and save it.
   ```
   python3 -m cuvm run experiments/smoke/config.json
   ```
   Expect about two minutes and `PASS 1 / 1`. If your plan is Pro, set
   `"plan": "pro"` in the config first.
5. Read the result in `runs/<run_id>/`: `record.json` (everything),
   `run.log` (phases), `results/screenshot-final.jpg`. One line per run is
   appended to `runs/runs.jsonl`. See [docs/records.md](docs/records.md).

## Commands

```
python3 -m cuvm run CONFIG [--out DIR] [--images DIR] [--repeat N] [--keep-on-fail]
python3 -m cuvm run --interactive [CONFIG] [--out DIR] [--images DIR] [--keep-on-fail]
python3 -m cuvm image record VM [--images DIR] [--settings FILE] [--source REF] [--guest-user NAME]
python3 -m cuvm image proxy VM [--out DIR]
```

- `run` defaults: output root `./runs`, image records `./images`.
  `--keep-on-fail` keeps a failed clone (stopped) for inspection, except when
  the guest-side login removal check failed; such a clone is deleted. A kept
  clone can only ever hold the run's dummy, which is useless once the run's
  proxy has stopped.
- `image record` hashes a stopped golden VM into `images/<vm>.json`. The
  guest's ssh host key is pinned by the first run and enforced after
  (`guest.host_key_pin` in the record says which). Re-recording an unchanged
  image keeps the pin; a changed image starts unpinned.
- `image proxy` runs the image-build proxy for a running golden VM, used only
  while building the image. It writes the build's CA certificate and dummy
  login and prints the commands that put them in the guest.

Run from the repository root: the defaults (`./runs`, `./images`) are relative
to the working directory. `pip install .` also gives a `cuvm` command, but the
clone is the tested path (the smoke config and image docs live in it).

Environment: `CUVM_KEYCHAIN_ITEM` (host keychain service holding the token,
default `cuvm-claude-oauth`; read only by the proxy process), `CUVM_GUEST_PW` (guest password, default
`admin`, the Tart base image default), `TART_HOME`.

## Interactive sessions

```
python3 -m cuvm run --interactive
```

Setup is the same as a scripted run (clone, proxy with the run's dummy login,
probes, the agent started in the guest's tmux with the config's arguments),
then your terminal is attached to that tmux pane and you use Claude Code as
usual; the Tart window shows the guest's screen. Without a config it uses
`experiments/interactive/config.json` (TextEdit, the Anthropic API only,
Claude's shell and file tools off, one hour). Copy it to change apps or
arguments.

- Exit Claude (`/exit` or Ctrl-D) to end the run: cuvm collects, removes the
  login, deletes the clone, checks for leaks and writes the record.
- You answer the agent's app-access dialogs yourself; cuvm never presses keys
  for you. It records the dialogs it sees by sampling the pane every two
  seconds, so one answered faster than that can be missed; what was granted
  comes from the transcript either way.
- Ctrl-b d detaches tmux; cuvm then asks whether to reattach or end. If your
  own terminal runs tmux, press its prefix twice to reach the guest's.
- While you are attached, Ctrl-C goes to Claude in the guest, not to cuvm. At
  cuvm's own prompt (after a detach) it interrupts cuvm: teardown still runs,
  but transcripts are not collected and the run is a FAIL (`operator_interrupt`).
  Answer `e` to end normally.
- At the time limit (`deadline_s`) the session is ended, with a warning in the
  pane five minutes before. cuvm also ends its own ssh to the pane, so the run
  reaches cleanup even if the guest does not answer; each stop attempt is in
  the record's `session_stops`.

The record has `"mode": "interactive"` and no task verdict: PASS or FAIL covers
containment and cleanup (probes, login checks, teardown, leaks, proxy log, no
forbidden tool executed). Apps you allowed beyond the config's list are
recorded in `artefacts.granted_outside_config`. `pane.log` holds the session as
your terminal saw it.

## Writing an experiment

Copy `experiments/smoke/`, edit `config.json` and put the files the agent may
read in `inputs/` (it must contain `known.txt`, which the read-only probe
hashes). Reference: [docs/config.md](docs/config.md).

## Layout

| path | contents |
| --- | --- |
| `cuvm/` | the harness (`run.py` lifecycle, `dialog.py` approval policy, `probes.py`, `session.py`, `guest.py`, `egress.py` (the host proxy), `transcript.py`, `image.py`, `config.py`) |
| `cuvm/scripts/` | login inject and removal, credential probe (shell, run by the harness) |
| `cuvm/data/` | allowlists, guest `/etc/zshenv` and Claude Code settings baseline |
| `experiments/smoke/` | the smoke experiment |
| `images/example.json` | shape of an image record |
| `tests/` | unit tests: `python3 -m unittest discover -s tests -v` |
| `docs/` | design, image runbook, config and record reference, findings |

For agents using this repository: [AGENTS.md](AGENTS.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
