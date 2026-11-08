# web-dev experiment

A Vite app runs inside the guest on loopback; the agent drives the guest browser against it and
writes what the page shows into TextEdit (docs/config.md, `project` and `dev_server`).

## Snapshot

`inputs/project.tgz` is not tracked. Build it on the host from `fixture/` (same CPU architecture
as the guest; the guest runs Node 24, see images/cuvm-browser.json):

```
cd experiments/web-dev/fixture
npm ci --cache ../.npm-cache          # first time, without a lockfile: npm install --cache ../.npm-cache
cd ../../..
python3 -m cuvm project pack experiments/web-dev/fixture experiments/web-dev/inputs/project.tgz
```

`pack` prints the archive's sha256 and file count; the run records the sha256 as
`project.archive_sha256`. The cache directory and `node_modules` are ignored by git.

Recorded snapshots:

- 2026-11-07: sha256 `64850840db7035a2b660320f03b57ad11930bac408c48c248bf3621b5a1edd3b`, 221 files, 10,838,373 bytes,
  from `npm install --cache ../.npm-cache` on the host (npm 11.12.1, node v26.0.0, arm64; vite 7 and 13 packages,
  `package-lock.json` created). esbuild's installer hard-links its native binary into two packages; pack stores
  the second as a plain copy. Top directory `fixture`.

## What a run checks

- D1 (pre): the dev server listens on `127.0.0.1:5173` only.
- D2 (post): nothing to `localhost`, `127.0.0.1` or `::1` was refused by the proxy while the agent
  ran (the browser's loopback bypass held).
- `guest_file_equals`: TextEdit holds `Submitted: WEBDEV-<run id>`, which only the page produces.
- `browser_navigated: localhost`.
- The record carries `project.patch`, `project-changed.tgz` (when the agent changed files),
  `dev-server-pane.txt`, and `dev-server.log` inside `results.tgz`.
- The project snapshot covers the whole `~/exp/project` root: a directory the agent creates beside
  `fixture/` (as the first interactive run did with `screensavers/`)
  is in `project.added` and `project-changed.tgz`; anything else it writes under the home, outside the
  documented exclusions (README, docs/design.md Known limits), comes back in `home-changed.tgz`
  (`home_changes` in docs/records.md). `negatives/output.json` is `shell.json` with a
  prompt that writes a sidecar directory and a Desktop note first, to check both paths.

## Interactive sessions

`dev.json` is `config.json` with `deadline_s` 1800 (30 minutes) and `tools` (Bash, Read, Write, Edit,
NotebookEdit, Glob, Grep: the agent edits the project directly instead of pasting through TextEdit), for
driving the agent by hand:

```
python3 -m cuvm run --interactive experiments/web-dev/dev.json
```

The time limit warning is a 15 s tmux message in the guest session at five minutes left. The
Playwright window opens behind the frontmost app and stays hidden while the agent works in another app
through computer use: click it in the guest Dock, or ask the agent for a `browser_take_screenshot` (no
access dialog).

## Shell run

`shell.json` is the scripted counterpart with `tools` Bash, Read and Edit: the agent changes the h1 with
Edit, greps it with Bash, opens the page in the Playwright browser, then types the heading into TextEdit
(the scripted judge needs a computer-use step). Expect `tools_used` to hold Edit, Bash, the Playwright and
computer-use tools, project.patch to be the h1 line, and `guest.settings_sha256` to differ from
`guest.settings_baseline_sha256`.
