# Building the golden image

The golden image is a stopped Tart VM that every run clones. You build it once
by hand, record its hashes, and never boot it again (any boot changes the disk
and the next run refuses it). To change it, build or clone a new one and record
that.

Run every host command from the repository root. `<ip>` is the guest address
from `tart ip cuvm-golden`; ssh uses a repo-local known_hosts file so your own
`~/.ssh/known_hosts` is untouched:

```
alias gssh='ssh -o UserKnownHostsFile=runs/known_hosts admin@<ip>'
mkdir -p runs
```

Guest steps marked (GUI) happen in the Tart window; all others go over ssh.

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

The proxy listens on the VM's gateway (usually 192.168.128.1) port 8888 and
allows only `cuvm/data/allow-imagebuild.txt` (Anthropic hosts for the Claude
Code installer, Homebrew and GitHub for tmux). macOS may ask once whether
tinyproxy may accept incoming connections: allow it.

## 3. ssh in (GUI, then host)

(GUI) System Settings, General, Sharing: turn on Remote Login.

```
ssh-copy-id -o UserKnownHostsFile=runs/known_hosts admin@<ip>    # password admin
gssh 'printf "PasswordAuthentication no\nKbdInteractiveAuthentication no\n" | sudo tee /etc/ssh/sshd_config.d/000-cuvm.conf && sudo launchctl kickstart -k system/com.openssh.sshd'
gssh 'echo "admin ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/cuvm && sudo chmod 440 /etc/sudoers.d/cuvm'
```

The key must work without a passphrase prompt (ssh runs with BatchMode).
Passwordless sudo is needed because each run rewrites the clone's network and
proxy settings. Do not install tart-guest-agent: it would give the host a
second, unaudited channel into the guest.

## 4. Proxy environment and paths

```
scp -o UserKnownHostsFile=runs/known_hosts cuvm/data/zshenv admin@<ip>:zshenv
gssh 'sudo mv zshenv /etc/zshenv && sudo chown root:wheel /etc/zshenv && printf "export PATH=/opt/homebrew/bin:\$HOME/.local/bin:\$PATH\n" >> ~/.zshenv'
gssh 'sudo networksetup -setmanual Ethernet <ip> 255.255.255.0 <gateway>; sudo networksetup -setdnsservers Ethernet <gateway>'
```

`/etc/zshenv` points every guest shell at the proxy (each run rewrites the
port in its clone) and turns off the Claude Code auto-updater. The manual
router and DNS setting is needed for the build only; without it macOS Local
Network privacy blocks `claude` from reaching the proxy (see
[findings.md](findings.md)). The ssh session drops when the network changes;
reconnect. Step 9 reverts it.

Do not add `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DISABLE_TELEMETRY` or
`DO_NOT_TRACK` anywhere in the guest: each one disables the feature-flag fetch
that turns computer use on. The proxy allowlist limits telemetry instead.

## 5. Power, Claude Code, tmux

```
gssh 'sudo pmset -a displaysleep 0 sleep 0 disksleep 0'
gssh 'curl -fsSL https://claude.ai/install.sh | bash && claude --version'
gssh 'brew install tmux'
gssh 'mkdir -p ~/exp/results ~/.claude'
scp -o UserKnownHostsFile=runs/known_hosts cuvm/data/guest-settings.json admin@<ip>:.claude/settings.json
```

(GUI) System Settings: screen saver never, and turn off requiring a password
after sleep or screen saver.

`guest-settings.json` is the guest Claude Code settings baseline: no auto
memory, `dontAsk` permission mode with bypass and auto modes disabled, only
ToolSearch and the computer-use MCP allowed, every other built-in tool denied.
Runs check that the guest file still hashes to the value in the image record.

## 6. Sign in for the build session, enable computer use

The login is never stored in the image. For each build step that needs
Claude, inject it, and remove it afterwards:

```
CUVM_KNOWN_HOSTS=runs/known_hosts cuvm/scripts/inject_login.sh <ip> max    # or pro
gssh -t 'security unlock-keychain -p admin ~/Library/Keychains/login.keychain-db && cd ~/exp && claude'
```

In that Claude session type `/mcp`, select `computer-use`, enable it, and exit.
The opt-in is saved for `~/exp` in `~/.claude.json`.

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

## 9. Clean, revert, shut down

```
gssh 'tmux kill-server; rm -f ~/.claude/projects/*/*.jsonl ~/.zsh_history ~/.claude/history.jsonl'
gssh 'rm -rf ~/.claude/projects/*/memory ~/exp/CLAUDE.md ~/exp/.claude ~/.claude/CLAUDE.md ~/exp/results/* ~/Desktop/*.txt'
gssh 'rm -rf ~/Library/Saved\ Application\ State/com.apple.TextEdit.savedState'
CUVM_KNOWN_HOSTS=runs/known_hosts cuvm/scripts/remove_login.sh <ip>         # exit 0 required
gssh 'sudo networksetup -setdhcp Ethernet; sudo networksetup -setdnsservers Ethernet empty'
```

`remove_login.sh` deletes the login and runs the credential probe (E2); every
line must say `ok`. Then shut down from the Apple menu (GUI), and stop the
build proxy with Ctrl-C.

## 10. Record

```
python3 -m cuvm image record cuvm-golden --source ghcr.io/cirruslabs/macos-tahoe-base@sha256:<digest>
```

`tart list` shows the digest of the pulled base image. The record goes to
`images/cuvm-golden.json` (git-ignored: it describes your machine's image).
If you installed a different guest settings file, pass `--settings`.

The next run pins the guest's ssh host key into the record; later runs fail if
it changes. Now run the smoke experiment (README, quickstart step 4).
