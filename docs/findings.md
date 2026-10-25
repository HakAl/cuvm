# Findings

Things learned building this, with Claude Code 2.1.310 on macOS 26 and Tart
2.40.1. Each one cost time; they are written down so the next person does not
pay again. Version-specific: recheck after upgrading.

## Computer use

- **Needs a stored claude.ai login, not an env token.** `CLAUDE_CODE_OAUTH_TOKEN`
  authenticates API calls but never unlocks computer use: the gate reads the
  plan type (`pro` or `max`) from the stored login object, and an env token
  disables that lookup. cuvm builds a login object from a `claude setup-token`
  token (no refresh token) and puts it in the guest's login keychain under
  `Claude Code-credentials` for the length of a run.
- **Three variables silently remove it.** `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`,
  `DISABLE_TELEMETRY` and `DO_NOT_TRACK` turn off feature-flag evaluation, and
  the computer-use server is gated on a flag. `/mcp` then shows no
  computer-use at all, with no error. Limit telemetry with the egress
  allowlist instead.
- **Opt-in is per project directory** and persists in `~/.claude.json`, so
  sessions must start in the directory where it was enabled (`~/exp`).
- **`--tools ToolSearch` keeps computer use.** It reduces built-in tools to
  ToolSearch, and the computer-use MCP tools are loaded through it. It is the
  primary tool control; the settings deny list and `--disallowedTools` are
  further layers. `dontAsk` mode alone does not remove tools, and some tools
  skip the permission check.
- **Unknown tool names in deny lists produce startup warnings.** Deny only
  names the installed version registers.
- **Grants are session memory only.** No setting pre-approves apps, so the
  harness answers the dialog itself, against the config.
- **The dialog has two layouts.** A reason that wraps is drawn with a `│`
  gutter; a one-line reason has none. A parser built from one sample rejects
  the other.
- **The agent may still try removed tools.** Observed: with Bash removed, the
  agent called Bash to check its saved file; Claude Code refused it ("No such
  tool available"). Records keep attempts separate from tools that ran.
- **Prompts must match what apps do on launch.** TextEdit opens an empty
  document when it starts; a prompt saying "make a new document" produced a
  second one.

## The guest

- **Local Network privacy blocks `claude` under tmux.** macOS blocks
  non-Apple binaries from reaching on-link addresses such as the proxy on the
  vmnet gateway. A process started straight from ssh inherits sshd's allowance;
  the tmux server daemonizes, loses it, and `claude` inside it is dropped
  silently (`EHOSTUNREACH`, logged as NECP). `curl` works everywhere because
  Apple binaries are exempt. The active DNS server address is exempt (Apple
  TN3179), and DNS only becomes active with a router set, so cuvm sets both
  router and DNS to the gateway per run (`networksetup -setmanual` and
  `-setdnsservers`). A router alone is not enough. Shown A/B/A in one boot.
- **The same check applies on the host.** The terminal app that runs cuvm needs
  Local Network access, or ssh to the guest fails with "No route to host".
- **The login keychain is locked for ssh sessions**, and an unlock lasts only
  for the ssh session that made it. Unlock and use it in one ssh command; a
  tmux server started in that command inherits the unlocked state.
- **Locked reads look like absence.** `security find-generic-password` on a
  locked keychain exits 36, not 44 (not found). A credential check that treats
  any non-zero exit as "absent" passes with a credential present.
- **macOS `grep` exits 2 on a match plus a missing path**, and an empty zsh glob
  aborts the command. Decide on grep's output, not its exit status.
- **`/etc/zshenv` is sourced by every zsh**, including non-interactive ssh and
  tmux commands, so it is where the proxy setting goes. It also overrides
  anything passed in the parent environment, so per-run values must be written
  into the clone's file (cuvm rewrites the port).
- **Two concurrent VMs get separate vmnet subnets** (192.168.128.0/24 and
  192.168.129.0/24 observed), so each run's gateway and proxy differ.
- **Do not change the guest password.** Auto-login and the login keychain
  both depend on it.
- **`tmux send-keys <text>` then a separate `C-m`** submits to Claude Code;
  `Enter` straight after the text did not.

## The host

- **Hash big images streamed.** The golden `disk.img` is 50 GB logical; reading
  it whole into memory to hash it nearly exhausted a 48 GB host.
- **Any boot changes the golden disk.** Never run the golden image after
  recording it; clone it.
