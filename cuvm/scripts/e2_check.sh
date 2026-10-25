#!/bin/zsh
# Probe E2 cred-clean, read-only. Exit 0 only if all four checks hold in the guest.
# Usage: CUVM_KNOWN_HOSTS=<file> e2_check.sh <guest-ip>. Prints one line per check; never prints a secret.
set -u
KH=${CUVM_KNOWN_HOSTS:?set CUVM_KNOWN_HOSTS to the known_hosts file for this run}
GUEST=${1:?usage: $0 guest-ip}
PW=${CUVM_GUEST_PW:-admin}
ssh -o BatchMode=yes -o UserKnownHostsFile="$KH" "${CUVM_GUEST_USER:-admin}@$GUEST" '
rc=0
if test ! -e ~/.claude/.credentials.json; then echo "E2.1 ok: no credentials.json"; else echo "E2.1 FAIL: credentials.json present"; rc=1; fi
# The keychain is locked for ssh sessions (exit 36 "User interaction is not allowed"), which
# would look like "absent". Unlock for this ssh session only, and count only 44 (not found) as ok.
security unlock-keychain -p "'"$PW"'" ~/Library/Keychains/login.keychain-db >/dev/null 2>&1
security find-generic-password -s "Claude Code-credentials" >/dev/null 2>&1; k=$?
if [ $k -eq 44 ]; then echo "E2.2 ok: no guest keychain item"; elif [ $k -eq 0 ]; then echo "E2.2 FAIL: guest keychain item present"; rc=1; else echo "E2.2 FAIL: keychain not readable (exit $k)"; rc=1; fi
security lock-keychain ~/Library/Keychains/login.keychain-db >/dev/null 2>&1
# Decide on output, not exit status: grep exits 2 on a match plus any missing path,
# and an empty zsh glob aborts the command; both would read as "ok". (N) drops absent paths.
hits=$(grep -rl "sk-ant-" ~/.claude.json(N) ~/.claude(N) ~/exp(N) ~/.zsh*(N) /etc/zshenv(N) </dev/null 2>/dev/null)
if [ -n "$hits" ]; then echo "E2.3 FAIL: sk-ant- in $(printf "%s\n" "$hits" | wc -l | tr -d " ") file(s): ${(f)hits}"; rc=1; else echo "E2.3 ok: no sk-ant- on disk"; fi
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then echo "E2.4 ok: env var empty"; else echo "E2.4 FAIL: env var set"; rc=1; fi
exit $rc'
