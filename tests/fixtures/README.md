# Test fixtures

All fixtures are committed and synthetic or reduced; tests read nothing else.

| file | origin |
| --- | --- |
| `transcript-smoke.jsonl` | a real smoke-run transcript reduced to tool calls and tool results; message envelopes (uuids, session id, cwd) dropped; each screenshot replaced by a short placeholder derived from its content |
| `dialog-textedit.txt`, `dialog-textedit-oneline.txt` | the Claude Code app-access dialog as captured from the pane, in its two layouts |
| `dialog-finder-warning.txt` | the access dialog from a Haiku L3 run (experiments/model-effort), [TextEdit, Finder] with Finder's `⚠ can read/write any file` line, as captured from the pane |
| `menu-mcp.txt` | the `/mcp` panel from operator run interactive-20261025T234032Z-012d, blank screen lines dropped; a menu, not an approval dialog |
| `codex/tui-ready.txt`, `codex/tui-working.txt` | the Codex 0.167.0 TUI ready screen and the working screen (`esc to interrupt`) from the probe clone, runs/codex-probe/tui.txt lines 2 to 46 and 54 to 98 (the probe transcript holds several panes and metadata under `===` headers, so only the panes are kept) |
| `codex/tui-approval.txt` | the Codex command approval prompt (approval on-request, a command outside the workspace) from runs/codex-probe/tui-approval.txt lines 2 to 20 |
| `codex/tui-complete.txt` | the Codex TUI after the approved command ran and the turn ended, runs/codex-probe/tui-complete.txt lines 2 to 19 |
| `codex/rollout-web-items.jsonl` | the nine item lines of the Codex rollout from the scripted L3 web run codex-20261108T145212Z-7aac (the retained live-run evidence L3): four `response_item` `custom_tool_call` records named `exec`, three `CommandExecution` and two `McpToolCall` `item_completed` events; `session_meta` (account and user ids) dropped, thread and turn ids replaced by `<thread>` and `<turn>`, item IDs replaced by synthetic labels, Playwright snapshot results replaced by `<cut>` |
| `codex/tui-approval-wrapped.txt`, `codex/tui-patch-approval.txt` | the two approval prompts of the interactive L5 run codex-20261108T190956Z-7c4e as recorded in `dialogs[].text` (the retained live-run evidence L5) plus the footer line the recorder strips: the command prompt with option 2 wrapped at the terminal width, and the apply_patch prompt for a file outside the writable roots |
| `codex/rollout-l5-items.jsonl` | the 17 item lines of the L5 rollout: six `custom_tool_call` records, five `CommandExecution`, two `FileChange`, two `McpToolCall` and one `Extension` (`web.search`) `item_completed` events and the last `token_count`; `session_meta` dropped, thread, turn and item ids replaced, outputs, results and the search results replaced by `<cut>`, the account state in `rate_limits` (`used_percent`, `resets_at`, `credits.balance`) replaced by synthetic values (Lookout R6-F2) |
| `codex/rollout-reasoning.jsonl` | reduced captured Codex `Reasoning` completion event; no account, session, summary or raw content retained |
