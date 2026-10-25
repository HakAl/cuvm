# Notes for agents working in this repository

cuvm runs Claude Code computer-use experiments in disposable Tart macOS VMs.
Read README.md first, then docs/design.md for the boundary and lifecycle.

## Running things

- Unit tests need nothing but Python 3.11+ and run anywhere:
  `python3 -m unittest discover -s tests -v`. They read only committed fixtures.
- `python3 -m cuvm run CONFIG` needs a macOS host with Tart, tinyproxy, a
  golden image built per docs/image.md, its record in `images/<vm>.json`, and
  the token in the host keychain. A run takes about two minutes, boots a VM
  and spends API usage on the operator's account: run it only when asked.
- At most two runs at once (Apple's limit of two macOS guests per host).
- Exit code 0 means PASS. The full story is in `runs/<run_id>/record.json`
  and `run.log` (docs/records.md).

## Rules that keep the boundary intact

- Never boot the golden image to try something. Any boot changes its disk and
  every later run refuses it until it is re-recorded. Work on clones.
- Never print, log or pass the token in argv. The scripts in `cuvm/scripts/`
  move it over ssh stdin only; keep it that way.
- Do not add shell or file tools to `agent_args` without revisiting the
  credentials section of docs/design.md: the login sits in the guest keychain
  during a run.
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
