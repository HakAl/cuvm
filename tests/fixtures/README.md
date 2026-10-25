# Test fixtures

All fixtures are committed and synthetic or reduced; tests read nothing else.

| file | origin |
| --- | --- |
| `transcript-smoke.jsonl` | a real smoke-run transcript reduced to tool calls and tool results; message envelopes (uuids, session id, cwd) dropped; each screenshot replaced by a short placeholder derived from its content |
| `tinyproxy.log` | a short tinyproxy log from a real run, host paths removed |
| `tinyproxy-expected.conf` | the config `proxy.render` produced for a real run, host paths replaced by `/cuvm/...` and `/runs/...` |
| `dialog-textedit.txt`, `dialog-textedit-oneline.txt` | the Claude Code app-access dialog as captured from the pane, in its two layouts |
