#!/bin/zsh
# Put the operator's Claude login into the guest's login KEYCHAIN for one session
# (encrypted at rest), built from the host keychain token, no refresh token.
# The secret travels over ssh stdin only: never in argv, a file, or this script's output.
# The guest keychain is locked for ssh sessions and an unlock lasts only for the ssh
# session that made it (observed 2026-10-25), so unlock and write share one ssh command,
# and the session that starts claude must unlock too (cuvm/session.py does). Guest password:
# Tart base default "admin", override with CUVM_GUEST_PW.
# Remove with remove_login.sh. Usage: CUVM_KNOWN_HOSTS=<file> inject_login.sh <guest-ip> [plan: max|pro]. Host token: keychain item $CUVM_KEYCHAIN_ITEM (default cuvm-claude-oauth).
set -u
set -o pipefail
KH=${CUVM_KNOWN_HOSTS:?set CUVM_KNOWN_HOSTS to the known_hosts file for this run}
GUEST=${1:?usage: $0 guest-ip}; PLAN=${2:-max}
security find-generic-password -s "${CUVM_KEYCHAIN_ITEM:-cuvm-claude-oauth}" -w | tr -d '\n' | ssh -o BatchMode=yes -o UserKnownHostsFile="$KH" "${CUVM_GUEST_USER:-admin}@$GUEST" "security unlock-keychain -p '${CUVM_GUEST_PW:-admin}' ~/Library/Keychains/login.keychain-db || { echo 'keychain unlock FAILED'; exit 1; }; python3 -c '
import json,subprocess,sys,time
tok=sys.stdin.read().strip()
if not tok.startswith(\"sk-ant-\"): sys.exit(\"host token missing or malformed, nothing written\")
blob=json.dumps({\"claudeAiOauth\":{\"accessToken\":tok,\"refreshToken\":None,\"expiresAt\":int((time.time()+360*86400)*1000),\"scopes\":[\"user:inference\",\"user:profile\"],\"subscriptionType\":\"$PLAN\"}},separators=(\",\",\":\"))
q=lambda v: chr(34)+v.replace(chr(92),chr(92)*2).replace(chr(34),chr(92)+chr(34))+chr(34)
cmd=\"add-generic-password -U -a \"+q(__import__(\"os\").environ[\"USER\"])+\" -s \"+q(\"Claude Code-credentials\")+\" -w \"+q(blob)+chr(10)
r=subprocess.run([\"security\",\"-i\"],input=cmd,text=True,capture_output=True)
sys.exit(r.returncode)
' && security find-generic-password -a \"\$USER\" -s 'Claude Code-credentials' >/dev/null 2>&1 && echo \"login keychain item written \$(date -u +%FT%TZ)\" || { echo 'login keychain write FAILED'; exit 1; }"
