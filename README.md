# cuvm

I wanted a place to try computer use without giving an agent access to my
everyday desktop. cuvm runs each experiment in a disposable macOS VM and
records its containment checks, output and cleanup.

| Try | What you get |
| --- | --- |
| [Claude Code](#claude-code) | Drive macOS apps such as TextEdit and optionally use Playwright for browser experiments. |
| [Codex](#codex-quickstart) | Use shell and file tools, with an optional local Playwright browser and dev server. |

Each run takes one command after [setup](#quickstart). cuvm clones a prepared
Tart image, restricts guest network access with a host proxy, collects results
and removes the clone. Scripted runs and interactive terminal sessions are
both supported. See [Interactive sessions](#interactive-sessions).

Status: v0.3.0, tested on one Apple silicon Mac with macOS 26 and Tart
2.40.1. Guest versions are Claude Code 2.1.310 and Codex CLI 0.167.0.
See the [release notes](docs/release-0.3.0.md).

## Why

Computer use lets a model see a screen and drive the mouse and keyboard. On
your own desktop, the boundary is your whole account. Here the boundary is a
throwaway VM that cannot see your files, can reach only the hosts you list, and
never holds your login token: the guest gets a dummy, and a proxy on the host
swaps in the real token on its way to the configured agent service.

## Safety model, in short

| layer | what it does | how it is checked each run |
| --- | --- | --- |
| VM | Tart clone of a golden image, deleted afterwards | golden hashes unchanged, clone gone |
| filesystem | one read-only share (`inputs/`), no host paths | probes F1-F4 |
| network | only a proxy on the host (`cuvm/egress.py`), allowlisted hosts, HTTPS to port 443 only; plain HTTP refused | probes N1-N3, proxy log |
| identity | guest has its own user, host name and keys | probe I1 |
| credentials | the real token stays on the host; the guest login holds a per-run dummy that the host proxy swaps for the real token in the Authorization header to api.anthropic.com only (with `codex`: a dummy JWT in the guest's `auth.json`, swapped at chatgpt.com) | probe E3 (guest holds the dummy), E2 before and after, proxy log |
| agent tools | Claude: by default built-in tools reduced to ToolSearch; shell and file tools denied. A config with `tools` enables the named shell or file tools, and the agent then acts as the guest user, so the remaining deny list is a default, not a boundary (docs/config.md, `tools`). Codex: shell/file tools are always available, with optional Playwright | transcript check (evidence, not enforcement, with `tools`) |
| app access | Claude: the access dialog is approved only for the config's apps and grants; a dialog outside the config is left open and ends the run, or with `on_refusal: deny` is denied and the run keeps watching | dialog record |

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
- `openssl` on `PATH` for the per-run proxy certificates. With Python 3.13+ use
  OpenSSL 3 ahead of macOS LibreSSL on `PATH`; the latter produces certificates
  rejected by the newer Python TLS defaults in the local test suite. This release
  is verified with Python 3.14.7 and OpenSSL 3.6.3.
- Python 3.11 or newer (standard library only) and zsh (the macOS default shell).
- The terminal app you run cuvm from needs Local Network access
  (System Settings, Privacy and Security, Local Network), or ssh to the guest
  fails with "No route to host".

## Quickstart

Choose your agent below. The one-time setup includes a prepared Tart image and
a host login for the agent. Run the commands from the repository root.

### Claude Code

Use this path for macOS desktop experiments. Computer use requires a Claude
Pro or Max plan. Use the host Claude Code CLI to generate the setup token.
The native tool can see the browser in screenshots but cannot click or type
there. For browser interaction, use the [Playwright workflow](docs/image.md#derived-browser-image).

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

### Codex quickstart

Use this path for shell and web development with a ChatGPT subscription.

1. Follow [local image setup](docs/image.md#local-codex-image-setup) to
   build and record `cuvm-codex` from a trusted local Tart base. Skip this
   step if you already have the recorded image.
2. If Codex is already signed in with ChatGPT on your Mac, cuvm can use that
   session. Otherwise run `codex login` on the Mac and choose ChatGPT.
3. Run the scripted smoke test. It writes one line to a file in the VM and
   checks the result.

   ```sh
   python3 -m cuvm run experiments/codex/smoke.json
   ```

   Look for `PASS 1 / 1` and the saved `record.json` under `runs/`.
4. For browser work, prepare the [web project snapshot](experiments/web-dev/README.md#snapshot)
   and run the interactive preset.

   ```sh
   python3 -m cuvm run --interactive experiments/codex/web.json
   ```

## Commands

```
python3 -m cuvm run CONFIG [--out DIR] [--images DIR] [--repeat N] [--keep-on-fail]
python3 -m cuvm run --interactive [CONFIG] [--out DIR] [--images DIR] [--keep-on-fail]
python3 -m cuvm image record VM [--images DIR] [--settings FILE] [--source REF] [--guest-user NAME]
                             [--browser-info FILE] [--codex-info FILE]
python3 -m cuvm image proxy VM [--out DIR] [--allow FILE]
python3 -m cuvm image prepare --agent codex --base NAME [--execute]
python3 -m cuvm project pack DIR OUT.tgz
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
- `image prepare` previews local Codex image setup. Add `--execute` to build
  and record it. See [local image setup](docs/image.md#local-codex-image-setup).
- `project pack` writes a project snapshot for a config's `project` key
  (docs/config.md): one top-level directory, `.git` and `.DS_Store` left out,
  `node_modules` kept, refused if any file holds `sk-ant-`. This is not a
  general credential scanner: exclude `auth.json`, JWTs and all other credentials yourself.

Run from the repository root: the defaults (`./runs`, `./images`) are relative
to the working directory. `pip install .` also gives a `cuvm` command, but the
clone is the tested path (the smoke config and image docs live in it).

Claude uses `cuvm-claude-oauth` unless `CUVM_KEYCHAIN_ITEM` selects another
host item. For Codex, leave `CUVM_KEYCHAIN_ITEM` unset to use the native
host login. An explicit nonblank value selects an existing legacy Keychain
item. A blank value produces an error for Codex. `CUVM_GUEST_PW` defaults to
the Tart base account password `admin`. Tart honors `TART_HOME`.

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
arguments. A config with `tools` (docs/config.md) gives the agent a shell and
file tools in the guest; `experiments/web-dev/dev.json` is one.

- Exit Claude (`/exit` or Ctrl-D) to end the run: cuvm collects, removes the
  login, deletes the clone, checks for leaks and writes the record. Collection
  brings back every regular file the agent created or changed under its home
  (`home-changed.tgz`, plus the project snapshot diff with `project`), except
  under the documented exclusions (`~/Library`, `~/.claude`, `~/.claude.json`
  and its backup, `~/.local/state/claude`, `~/.cuvm`, `~/.npm`, `~/.cache`,
  `~/.Trash`, `~/Pictures/Photos Library.photoslibrary`, any `node_modules`,
  `.DS_Store` files): a file saved there is lost with the clone
  and no failure says so (docs/records.md `home_changes`, docs/design.md Known
  limits). Collection also runs when the run ends in an
  error or an interrupt after the agent started.
- You answer the agent's app-access dialogs yourself; cuvm never presses keys
  for you. It records the dialogs it sees by sampling the pane every two
  seconds, so one answered faster than that can be missed; what was granted
  comes from the transcript either way.
- Ctrl-b d detaches tmux; cuvm then asks whether to reattach or end. If your
  own terminal runs tmux, press its prefix twice to reach the guest's.
- While you are attached, Ctrl-C goes to Claude in the guest, not to cuvm. At
  cuvm's own prompt (after a detach) it interrupts cuvm: the project, home and
  results collectors still run before teardown (`salvage` in the record),
  transcripts are not collected and the run is a FAIL (`operator_interrupt`).
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

## Codex

`"agent": "codex"` runs Codex CLI 0.167.0 in the guest in place of Claude
Code. It uses shell and file tools inside Codex's sandbox and supports optional
Playwright browser tools. It uses the same clone, proxy, probes and record.

1. Prepare and record `cuvm-codex` using the
   [local setup](docs/image.md#local-codex-image-setup) or the
   [manual route](docs/image.md#derived-codex-image).
2. Sign in once with `codex login` on the Mac if you haven't already signed
   in with ChatGPT. Each run uses that host session through the isolated
   proxy. If macOS prompts for access to Codex's Keychain item, choose
   **Allow** for that read. If cuvm reports a missing or expired session,
   open Codex on the host and complete any sign-in it requests, then rerun.
   See [supported host login storage](docs/design.md#credentials).
3. Run. `net_allow` must list `chatgpt.com` (`experiments/codex/allow.txt` does).
   ```
   python3 -m cuvm run experiments/codex/smoke.json
   python3 -m cuvm run experiments/codex/web.json              # Playwright, project snapshot, dev server
   python3 -m cuvm run --interactive experiments/codex/web.json
   python3 -m cuvm run --interactive experiments/codex/web-30m.json # 30 minutes at the terminal
   ```

The 30-minute browser preset starts its clock at terminal attachment; VM startup
and collection take additional time. Enter your own prompts. PASS covers the
harness checks and cleanup. Project edits, new directories and home-file
changes are collected before the clone is deleted.

What differs from a Claude run: docs/config.md (Codex), docs/records.md
(`codex`, the `artefacts` keys, probes C1 and C2) and docs/design.md
(Credentials, Known limits). Codex's approval prompt is its own control: cuvm
records it in interactive runs and never answers it.

## Writing an experiment

Copy `experiments/smoke/`, edit `config.json` and put the files the agent may
read in `inputs/` (it must contain `known.txt`, which the read-only probe
hashes). Reference: [docs/config.md](docs/config.md).

## Layout

| path | contents |
| --- | --- |
| `cuvm/` | the harness (`run.py` lifecycle, `dialog.py` approval policy, `probes.py`, `session.py`, `guest.py`, `egress.py` (the host proxy), `transcript.py`, `image.py`, `config.py`) |
| `cuvm/scripts/` | login inject and removal, credential probe (shell, run by the harness) |
| `cuvm/data/` | allowlists (runs, golden, browser and codex image builds), guest `/etc/zshenv` and Claude Code settings baseline |
| `experiments/smoke/` | the smoke experiment |
| `experiments/web-dev/` | a dev server in the guest driven through the guest browser (`project`, `dev_server`); its README builds the snapshot |
| `experiments/codex/` | Codex CLI as the guest agent: smoke, web and negative configs |
| `images/example.json` | shape of an image record |
| `tests/` | unit tests: `python3 -m unittest discover -s tests -v` |
| `docs/` | design, image runbook, config and record reference, findings |

For agents using this repository: [AGENTS.md](AGENTS.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
