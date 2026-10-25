# Notes for agents working in this repository

cuvm runs Claude Code computer-use experiments in disposable Tart macOS VMs.
Read README.md first, then docs/design.md for the boundary and lifecycle.

## Running things

- Unit tests need only Python 3.11+ and zsh:
  `python3 -m unittest discover -s tests -v`. They read only committed fixtures.
- `python3 -m cuvm run CONFIG` needs a macOS host with Tart, openssl, a
  golden image built per docs/image.md, its record in `images/<vm>.json`, and
  the token in the host keychain. A run takes about two minutes, boots a VM
  and spends API usage on the operator's account: run it only when asked.
- At most two runs at once (Apple's limit of two macOS guests per host).
- Exit code 0 means PASS. The full story is in `runs/<run_id>/record.json`
  and `run.log` (docs/records.md).

## Rules that keep the boundary intact

- Never boot the golden image to try something. Any boot changes its disk and
  every later run refuses it until it is re-recorded. Work on clones.
- The real token is read only by `cuvm/egress.py`, in its own process, and
  never enters the guest; the guest login holds a per-run dummy. Keep it that
  way: never print, log or pass either token in argv, and keep the swap limited
  to the auth headers on `api.anthropic.com` (docs/design.md, Credentials).
- Do not add shell or file tools to `agent_args` without revisiting the
  credentials section of docs/design.md: any guest process can make API calls
  on the account through the proxy while a run lasts.
- The approval policy in `cuvm/dialog.py` refuses anything it does not
  recognize. Widen it only with a captured dialog added under
  `tests/fixtures/` and a test.
- Do not set `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DISABLE_TELEMETRY` or `DO_NOT_TRACK`
  in the guest; each switches off computer use (docs/findings.md).

## Changing code

- Standard library only. Keep `python3 -m cuvm` working from a clone.
- Packaged assets live in `cuvm/scripts/` and `cuvm/data/`; user paths
  (`--out`, `--images`, config paths) resolve against the working directory
  or the config file, never the package.
- Add or update a unit test with each change; the harness's real behavior is
  verified by a smoke run, whose run id and result belong in the change
  description.
