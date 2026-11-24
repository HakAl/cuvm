# cuvm 0.3.0

Codex setup takes fewer steps. Prepare a shell and Playwright image from a
trusted local Tart base, then run it using the ChatGPT session from Codex on
your Mac. The guest receives a per-run dummy and the host proxy handles
authentication. [Start with the Codex quickstart](../README.md#codex-quickstart).

- `image prepare --agent codex` previews the build. Add `--execute` to
  provision a clone, install the pinned Codex and Playwright tools, and
  record the resulting stopped image. Image preparation needs no agent login.
- Upgrading from 0.2.0? Unset `CUVM_KEYCHAIN_ITEM` for Codex to use the
  host session. A nonblank value still selects an existing legacy Keychain
  item. A blank value raises an error. Claude still uses a host Keychain item.
- Scripted Claude runs paste the full prompt and compare it to the first
  user turn in the transcript. A transcript mismatch fails the run. If the
  agent returns to its input prompt without the done file, the run ends with
  `idle_without_done` instead of waiting for the experiment deadline.

## Verification

On one Apple silicon Mac, local provisioning completed from a stock Tahoe
base and a separate guest WebKit check passed. The authenticated Codex file
task passed on a disposable clone of that image using the existing host
sign-in. All 58 probes passed, the proxy and clone were removed, and the
golden image hashes matched.

The Claude change has unit coverage and an isolated tmux paste check with
a synthetic multiline prompt. The exported release tree has not been used
for a live guest run. Playwright and the native login were tested separately.
