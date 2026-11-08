# Test fixtures

All fixtures are committed and synthetic or reduced; tests read nothing else.

| file | origin |
| --- | --- |
| `transcript-smoke.jsonl` | a real smoke-run transcript reduced to tool calls and tool results; message envelopes (uuids, session id, cwd) dropped; each screenshot replaced by a short placeholder derived from its content |
| `dialog-textedit.txt`, `dialog-textedit-oneline.txt` | the Claude Code app-access dialog as captured from the pane, in its two layouts |
| `dialog-finder-warning.txt` | the access dialog from a Haiku L3 run (experiments/model-effort), [TextEdit, Finder] with Finder's `⚠ can read/write any file` line, as captured from the pane |
| `menu-mcp.txt` | the `/mcp` panel from operator run interactive-20261025T234032Z-012d, blank screen lines dropped; a menu, not an approval dialog |
