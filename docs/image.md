# Building the golden image

The golden image is a stopped Tart VM that every run clones. You build it once
by hand, record its hashes, and never boot it again (any boot changes the disk
and the next run refuses it). To change it, build or clone a new one and record
that.

Run every host command from the repository root, in one terminal (the build
proxy gets a second one). Guest steps marked (GUI) happen in the Tart window;
all others go over ssh, with a known_hosts file under `runs/` so your own
`~/.ssh/known_hosts` is untouched. The base image already has Homebrew, and
passwordless sudo for `admin`.

## 1. Pull and size

```
tart clone ghcr.io/cirruslabs/macos-tahoe-base:latest cuvm-golden
tart set cuvm-golden --cpu 4 --memory 8192
```

The base image logs in automatically as `admin` (password `admin`). Do not
change the password: auto-login and the login keychain depend on it, and the
guest is reachable only from your host.

## 2. Boot with the run flags and start the build proxy

```
tart run cuvm-golden --net-host --no-clipboard --no-audio --no-usb-accessories &
python3 -m cuvm image proxy cuvm-golden        # second terminal, leave running
```

Back in the first terminal, once `tart ip` answers:

```
IP=$(tart ip cuvm-golden); GW=${IP%.*}.1; echo "$IP $GW"
alias gssh="ssh -o UserKnownHostsFile=runs/known_hosts admin@$IP"
mkdir -p runs
```

`$GW` is the gateway that `image proxy` printed. If you are rebuilding an
image, first drop the old guest's key: `ssh-keygen -R "$IP" -f runs/known_hosts`.

The proxy (`cuvm/egress.py`, as in runs) listens on the VM's gateway port 8888
and allows only `cuvm/data/allow-imagebuild.txt` (Anthropic hosts for the
Claude Code installer, Homebrew and GitHub for tmux), HTTPS only. It reads your
token from the host keychain and, as in runs, swaps it in for a dummy on
requests to `api.anthropic.com`; other hosts are tunnelled untouched. It writes
`runs/image-build-<vm>/ca/ca.pem` and `runs/image-build-<vm>/dummy.token` (the
dummy, deleted when the proxy stops) and prints the step 6 commands. macOS may
ask once whether Python may accept incoming connections: allow it.

## 3. ssh in (GUI, then host)

(GUI) System Settings, General, Sharing: turn on Remote Login.

`ssh-copy-id` installs your default public key (`~/.ssh/id_ed25519.pub` or
similar; make one with `ssh-keygen -t ed25519` if you have none). Runs use
ssh's default identities with BatchMode, so the key must have no passphrase or
be loaded in ssh-agent.

```
ssh-copy-id -o UserKnownHostsFile=runs/known_hosts admin@$IP    # password admin
gssh 'printf "PasswordAuthentication no\nKbdInteractiveAuthentication no\n" | sudo tee /etc/ssh/sshd_config.d/000-cuvm.conf && sudo launchctl kickstart -k system/com.openssh.sshd'
gssh 'echo "admin ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/cuvm && sudo chmod 440 /etc/sudoers.d/cuvm'
```

The sudoers line makes the base image's passwordless sudo explicit; each run
needs it to rewrite the clone's network and proxy settings.

Remove the Tart guest agent, which the base image ships: it gives the host a
second, unaudited channel into the guest.

```
gssh 'setopt nullglob; for p in /Library/LaunchDaemons/*guest-agent*.plist; do sudo launchctl bootout system "$p"; done; for p in /Library/LaunchAgents/*guest-agent*.plist; do launchctl bootout gui/$(id -u) "$p"; done; sudo rm -f /Library/LaunchDaemons/*guest-agent*.plist /Library/LaunchAgents/*guest-agent*.plist /opt/homebrew/bin/tart-guest-agent'
gssh 'pgrep -fl guest-agent || echo none'      # must print: none
```

## 4. Proxy environment and paths

```
scp -o UserKnownHostsFile=runs/known_hosts cuvm/data/zshenv admin@$IP:zshenv
gssh 'sudo mv zshenv /etc/zshenv && sudo chown root:wheel /etc/zshenv && printf "export PATH=/opt/homebrew/bin:\$HOME/.local/bin:\$PATH\n" >> ~/.zshenv'
gssh "sudo nohup sh -c 'networksetup -setmanual Ethernet $IP 255.255.255.0 $GW; networksetup -setdnsservers Ethernet $GW' >/dev/null 2>&1 &"
gssh 'networksetup -getinfo Ethernet; networksetup -getdnsservers Ethernet'
```

`/etc/zshenv` points every guest shell at the proxy (each run rewrites the
port in its clone), makes Claude Code trust the proxy's CA at
`~/.cuvm/ca.pem`, and turns off the Claude Code auto-updater. The manual
router and DNS setting is needed for the build only; without it macOS Local
Network privacy blocks `claude` from reaching the proxy (see
[findings.md](findings.md)). The change runs detached because it can drop the
ssh session; if the check fails to connect, wait a few seconds and repeat it.
It must show `Manual Configuration`, `Router: $GW` and `$GW` as the only DNS
server. Step 9 reverts it.

Do not add `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DISABLE_TELEMETRY` or
`DO_NOT_TRACK` anywhere in the guest: each one disables the feature-flag fetch
that turns computer use on. The proxy allowlist limits telemetry instead.

## 5. Power, Claude Code, tmux

```
gssh 'sudo pmset -a displaysleep 0 sleep 0 disksleep 0'
gssh 'curl -fsSL https://claude.ai/install.sh | bash && claude --version'
gssh 'brew install tmux'
gssh 'mkdir -p ~/exp/results ~/.claude'
scp -o UserKnownHostsFile=runs/known_hosts cuvm/data/guest-settings.json admin@$IP:.claude/settings.json
```

(GUI) System Settings, Lock Screen: "Start Screen Saver when inactive" Never,
"Turn display off when inactive" Never, "Require password after screen saver
begins or display is turned off" Never.

`guest-settings.json` is the guest Claude Code settings baseline: no auto
memory, `dontAsk` permission mode with bypass and auto modes disabled, only
ToolSearch and the computer-use MCP allowed, every other built-in tool denied.
Runs check that the guest file still hashes to the value in the image record.

## 6. Sign in for the build session, enable computer use

Your token never enters the build VM: the guest gets the build proxy's dummy
login and CA certificate (the commands `image proxy` printed), and the proxy
adds the real token on the host. For each build step that needs Claude, inject
the dummy, and remove it afterwards (step 9):

```
D=runs/image-build-cuvm-golden
gssh 'mkdir -p ~/.cuvm && chmod 700 ~/.cuvm && cat > ~/.cuvm/ca.pem' < $D/ca/ca.pem
CUVM_KNOWN_HOSTS=runs/known_hosts cuvm/scripts/inject_login.sh "$IP" max < $D/dummy.token    # or pro
gssh -t 'security unlock-keychain -p admin ~/Library/Keychains/login.keychain-db && cd ~/exp && claude'
```

If this first launch asks for a theme or whether to trust `~/exp`, pick any
theme and trust the folder: runs start `claude` in `~/exp` and stop on any
question the harness does not recognize. Then type `/mcp`, select
`computer-use`, enable it, and exit. The opt-in is saved for `~/exp` in
`~/.claude.json`.

## 7. macOS privacy grants, through the run route (GUI)

Screen Recording and Accessibility must be granted to the process that runs
`claude` under tmux, so trigger them the same way a run does:

```
gssh 'security unlock-keychain -p admin ~/Library/Keychains/login.keychain-db && tmux new -d -s g -c ~/exp claude'
gssh "tmux send-keys -t g -l 'Take a screenshot, then open TextEdit and type TEST.'" && gssh 'tmux send-keys -t g C-m'
gssh -t 'tmux attach -t g'
```

Approve the TextEdit request in the attached pane, and in the Tart window grant
Screen Recording and Accessibility when macOS asks. Detach (Ctrl-b d), kill the
session (`gssh 'tmux kill-server'`), start it again and repeat the prompt. The
second time there must be no macOS prompt and the screenshot must show
TextEdit. If not, the image is not ready.

## 8. TextEdit defaults

```
gssh 'defaults write com.apple.TextEdit RichText -int 0; defaults write com.apple.TextEdit AddExtensionToNewPlainTextFiles -bool false'
```

(GUI) Open TextEdit, dismiss any first-launch dialog, save one document to the
Desktop so the Save panel remembers it, quit, delete the file.

## Ready check

All of these must hold before you clean up:

```
ssh -o BatchMode=yes -o UserKnownHostsFile=runs/known_hosts admin@$IP true && echo ssh ok
gssh 'sudo -n true && echo sudo ok'
gssh 'pgrep -fl guest-agent || echo no guest agent'
gssh 'grep -q computer-use ~/.claude.json && echo computer-use opted in'
gssh 'shasum -a 256 ~/.claude/settings.json'; shasum -a 256 cuvm/data/guest-settings.json   # equal
```

and the second pass of step 7 showed no macOS prompt.

## 9. Clean, revert, shut down

```
gssh 'tmux kill-server; rm -f ~/.claude/projects/*/*.jsonl ~/.zsh_history ~/.claude/history.jsonl'
gssh 'rm -rf ~/.claude/projects/*/memory ~/exp/CLAUDE.md ~/exp/.claude ~/.claude/CLAUDE.md ~/exp/results/* ~/Desktop/*.txt'
gssh 'rm -rf ~/Library/Saved\ Application\ State/com.apple.TextEdit.savedState'
CUVM_KNOWN_HOSTS=runs/known_hosts cuvm/scripts/remove_login.sh "$IP"         # exit 0 required
gssh 'rm -rf ~/.cuvm'
gssh "sudo nohup sh -c 'networksetup -setdhcp Ethernet; networksetup -setdnsservers Ethernet empty' >/dev/null 2>&1 &"
gssh 'networksetup -getinfo Ethernet; networksetup -getdnsservers Ethernet'
```

The check must show `DHCP Configuration` and no DNS servers (reconnect as in
step 4 if needed; the address normally stays the same).

`remove_login.sh` deletes the login and runs the credential probe (E2); every
line must say `ok`. Then shut down (`gssh 'sudo shutdown -h now'`), wait for
`tart list` to show it stopped, and stop the build proxy with Ctrl-C.

## 10. Record

```
python3 -m cuvm image record cuvm-golden --source ghcr.io/cirruslabs/macos-tahoe-base@sha256:<digest>
```

`tart list` shows the digest of the pulled base image. The record goes to
`images/cuvm-golden.json` (git-ignored: it describes your machine's image).
If you installed a different guest settings file, pass `--settings`.

The next run pins the guest's ssh host key into the record; later runs fail if
it changes. Re-recording an unchanged image keeps the pin and source; a changed
image starts unpinned. Now run the smoke experiment (README, quickstart step 4).

## Derived browser image

For configs with `"browser"` (docs/config.md). It is a clone of the golden
image plus a pinned Playwright MCP server and its WebKit build; the golden
image is not booted or changed. Steps as run on 2026-10-26 (Playwright MCP
0.0.83, WebKit 2368, Node 24.22.0 from the base image):

```
tart clone cuvm-golden cuvm-browser
tart run cuvm-browser --net-host --no-clipboard --no-audio --no-usb-accessories &
python3 -m cuvm image proxy cuvm-browser --allow cuvm/data/allow-imagebuild-browser.txt   # second terminal
IP=$(tart ip cuvm-browser); GW=${IP%.*}.1
```

The clone has the golden image's ssh host key: check `ssh-keyscan -t ed25519 $IP | ssh-keygen -lf -`
against `ssh_host_key_fp` in `images/cuvm-golden.json`, and use a known_hosts
file that holds only that key (`gssh` below). Set the build network as in step 4,
then:

```
gssh 'npm install -g @playwright/mcp@0.0.83'
gssh '"$(npm root -g)/@playwright/mcp/node_modules/.bin/playwright" install webkit'
gssh 'shasum -a 256 "$(npm root -g)/@playwright/mcp/cli.js"'
gssh 'cat > ~/launch-check.js' < experiments/browser/launch-check.js
gssh 'node ~/launch-check.js; node ~/launch-check.js https://example.com/; rm ~/launch-check.js'
```

The bundled `playwright` CLI installs the browser revision that the MCP
server's own Playwright expects. The launch check opens a headed WebKit window
through the build proxy: the first URL must print `status 200`, the second
(not on the build allowlist) must fail. Compare `launchctl list` and the
LaunchAgents/LaunchDaemons directories before and after: nothing may be added.
Chromium is not installed: its download host returned 403 and its fallback,
storage.googleapis.com, is not on the build allowlist.

Clean and shut down: `gssh 'rm -rf ~/.npm ~/.zsh_history ~/Library/Caches/org.webkit.Playwright'`,
revert the network as in step 9, check `~/.claude/settings.json` still hashes to
`cuvm/data/guest-settings.json`, `sudo shutdown -h now`, stop the proxy with Ctrl-C.
Then write the browser facts to a file and record:

```
{"node": "/opt/homebrew/bin/node", "node_version": "v24.22.0", "playwright_mcp": "0.0.83",
 "entry": "/opt/homebrew/lib/node_modules/@playwright/mcp/cli.js", "entry_sha256": "<from shasum>",
 "engines": {"webkit": "webkit-2368"}}

python3 -m cuvm image record cuvm-browser --browser-info browser-info.json --source "clone of cuvm-golden"
```

Runs refuse a `browser` config whose image record lacks the engine, and fail
probe B1 if the entry point's sha256 differs from the record.
