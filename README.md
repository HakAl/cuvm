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

Status: v0.1, tested on one host (Apple silicon, macOS 26, Tart 2.40.1,
tinyproxy 1.11.3, Claude Code 2.1.310 in the guest). Only the `claude` agent is
implemented.

## Why

Computer use lets a model see a screen and drive the mouse and keyboard. On
your own desktop, the boundary is your whole account. Here the boundary is a
throwaway VM that cannot see your files, can reach only the hosts you list, and
holds your login only while a run is in progress.

## Safety model, in short

| layer | what it does | how it is checked each run |
| --- | --- | --- |
| VM | Tart clone of a golden image, deleted afterwards | golden hashes unchanged, clone gone |
| filesystem | one read-only share (`inputs/`), no host paths | probes F1-F4 |
| network | only an HTTPS proxy on the host, allowlisted hosts, port 443 | probes N1-N3, proxy log |
| identity | guest has its own user, host name and keys | probe I1 |
| credentials | login put into the guest keychain per run, removed after | probe E2 before and after |
| agent tools | built-in tools reduced to ToolSearch; shell and file tools denied | transcript check |
| app access | the access dialog is approved only for the config's apps | dialog record |

What it does not protect: anything the allowlisted hosts can do with your
account, and the guest-to-host ports listed in the record (probe N3 measures
them but does not block them). Details and limits: [docs/design.md](docs/design.md).

## Requirements

- Apple silicon Mac on macOS 26 (the only host version tested), about 60 GB
  free disk on APFS.
- [Tart](https://tart.run) and tinyproxy: `brew install cirruslabs/cli/tart tinyproxy`.
- Python 3.11 or newer (standard library only).
- A Claude Pro or Max plan (computer use needs one) and a long-lived token
  from `claude setup-token`.
- The terminal app you run cuvm from needs Local Network access
  (System Settings, Privacy and Security, Local Network), or ssh to the guest
  fails with "No route to host".

## Quickstart

1. Build the golden image once, by following [docs/image.md](docs/image.md)
   (about an hour, mostly downloads; one GUI session for the macOS grants).
2. Store your token in the host keychain (interactive prompt, so the token
   stays out of shell history):
   ```
   claude setup-token
   security add-generic-password -a "$USER" -s cuvm-claude-oauth -w
   ```
3. Record the image (it must be stopped):
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
python3 -m cuvm image record VM [--images DIR] [--settings FILE] [--source REF] [--guest-user NAME]
python3 -m cuvm image proxy VM [--out DIR]
```

- `run` defaults: output root `./runs`, image records `./images`.
  `--keep-on-fail` keeps a failed clone (stopped) for inspection, except when
  the login could not be removed from it; such a clone is always deleted.
- `image record` hashes a stopped golden VM into `images/<vm>.json`. The
  guest's ssh host key is pinned by the first run and enforced after.
- `image proxy` runs the image-build proxy for a running golden VM, used only
  while building the image.

Environment: `CUVM_KEYCHAIN_ITEM` (host keychain service holding the token,
default `cuvm-claude-oauth`), `CUVM_GUEST_PW` (guest password, default
`admin`, the Tart base image default), `TART_HOME`.

## Writing an experiment

Copy `experiments/smoke/`, edit `config.json` and put the files the agent may
read in `inputs/` (it must contain `known.txt`, which the read-only probe
hashes). Reference: [docs/config.md](docs/config.md).

## Layout

| path | contents |
| --- | --- |
| `cuvm/` | the harness (`run.py` lifecycle, `dialog.py` approval policy, `probes.py`, `session.py`, `guest.py`, `proxy.py`, `transcript.py`, `image.py`, `config.py`) |
| `cuvm/scripts/` | login inject and removal, credential probe (shell, run by the harness) |
| `cuvm/data/` | proxy template, allowlists, guest `/etc/zshenv` and Claude Code settings baseline |
| `experiments/smoke/` | the smoke experiment |
| `images/example.json` | shape of an image record |
| `tests/` | unit tests: `python3 -m unittest discover -s tests -v` |
| `docs/` | design, image runbook, config and record reference, findings |

For agents using this repository: [AGENTS.md](AGENTS.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
