#!/bin/zsh
# Remove the injected login from the guest (keychain item, and the older file form),
# then run the strict E2 probe. Exit status is E2's: non-zero if anything credential-like remains.
set -u
KH=${CUVM_KNOWN_HOSTS:?set CUVM_KNOWN_HOSTS to the known_hosts file for this run}
GUEST=${1:?usage: $0 guest-ip}
PW=${CUVM_GUEST_PW:-admin}
ssh -o BatchMode=yes -o UserKnownHostsFile="$KH" "${CUVM_GUEST_USER:-admin}@$GUEST" 'security unlock-keychain -p "'"$PW"'" ~/Library/Keychains/login.keychain-db; while security delete-generic-password -s "Claude Code-credentials" >/dev/null 2>&1; do :; done; rm -f ~/.claude/.credentials.json; security lock-keychain ~/Library/Keychains/login.keychain-db; echo "login removed, keychain locked $(date -u +%FT%TZ)"'
exec "$(dirname "$0")/e2_check.sh" "$GUEST"
