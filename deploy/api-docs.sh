#!/usr/bin/env bash
# Open the engine's API documentation from a deployed instance, safely.
#
#     ./api-docs.sh ubuntu@1.2.3.4 -i ~/.ssh/key.pem
#     ./api-docs.sh ubuntu@1.2.3.4 -i ~/.ssh/key.pem --port 9000
#
# Then open http://localhost:8899/docs in a browser. Ctrl-C closes the tunnel.
#
# ## Why this exists rather than just publishing /docs
#
# The analyzer is not reachable from the internet, on purpose, and `/docs` is
# the sharpest reason why: it is not a reference page, it is an interactive form
# that composes and executes SQL against whichever customer database the
# connection points at, with a Try-it-out button next to every endpoint.
# `deploy/verify.sh` asserts it answers 404 from the public address, and that
# assertion is protecting something real.
#
# So the documentation is not published. It is reached the way anything else
# private is reached: over ssh, by someone who already has the key.
#
# This forwards a local port straight to the analyzer container's address on the
# Docker network. Nothing is published on the instance, no port is opened, no
# configuration changes, and the tunnel exists only while this runs. The
# container's address is looked up each time rather than hardcoded, because it
# changes whenever the stack is recreated.
#
# ## The offline alternative
#
# `contracts/openapi.json` in this repository is the same specification, checked
# in and version-controlled, and CI fails if it drifts from the code. If you
# want to read the contract rather than call it, read that - no instance and no
# ssh key required.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

LOCAL_PORT=8899
TARGET=""
SSH_OPTS=()

while [[ $# -gt 0 ]]; do
	case "$1" in
		--port) LOCAL_PORT="${2:?--port needs a number}"; shift 2 ;;
		-h|--help) sed -n '2,10p' "$0"; exit 0 ;;
		*)
			if [[ -z "$TARGET" && "$1" != -* ]]; then TARGET="$1"; else SSH_OPTS+=("$1"); fi
			shift ;;
	esac
done

[[ -n "$TARGET" ]] || die "Usage: ./api-docs.sh user@host [ssh options...] [--port N]
Example: ./api-docs.sh ubuntu@1.2.3.4 -i ~/.ssh/switchboard.pem"

printf '%sEngine API docs%s  via %s\n' "$C_BOLD" "$C_RESET" "$TARGET"

section "Locating the analyzer"

if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "${SSH_OPTS[@]}" "$TARGET" true 2>/dev/null; then
	die "Cannot ssh to $TARGET non-interactively.
Add -i /path/to/key.pem, or load the key into your agent: ssh-add /path/to/key.pem"
fi
pass "ssh works"

# Looked up live. A container address is assigned at creation, so it changes on
# every `docker compose up` that recreates the service - a value pasted into a
# command last week is a value that silently forwards to nothing today.
REMOTE_LOOKUP='cd ~/fraud-analyzer-engine/deploy 2>/dev/null || exit 1
cid=$(docker compose --env-file .env.prod -f docker-compose.prod.yml ps -q analyzer 2>/dev/null)
[ -n "$cid" ] || exit 2
docker inspect -f "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}" "$cid"'

CONTAINER_IP="$(ssh "${SSH_OPTS[@]}" "$TARGET" "$REMOTE_LOOKUP" 2>/dev/null || true)"
CONTAINER_IP="$(printf '%s' "$CONTAINER_IP" | tr -d '[:space:]')"

if [[ -z "$CONTAINER_IP" ]]; then
	die "Could not find a running analyzer container on $TARGET.
Check the stack is up:  ssh $TARGET 'cd ~/fraud-analyzer-engine/deploy && ./verify.sh'"
fi
pass "analyzer container is at $CONTAINER_IP on the instance's Docker network"

if ss -ltnH "sport = :$LOCAL_PORT" 2>/dev/null | grep -q .; then
	die "Local port $LOCAL_PORT is already in use. Pick another: --port 9001"
fi

section "Forwarding"

cat <<EOF

  ${C_BOLD}http://localhost:${LOCAL_PORT}/docs${C_RESET}          interactive API reference
  http://localhost:${LOCAL_PORT}/redoc          the same thing, easier to read
  http://localhost:${LOCAL_PORT}/openapi.json   the raw specification

  This is a private tunnel. Nothing was published on the instance, and it
  closes when you press Ctrl-C.

  Calling an endpoint from that page still needs a session token - the engine
  authenticates every request whether or not it arrived through a tunnel.

EOF

# Foreground on purpose: the tunnel's lifetime is this command's lifetime, so
# Ctrl-C closes it and nothing is left forwarding in the background afterwards.
exec ssh -N "${SSH_OPTS[@]}" -L "${LOCAL_PORT}:${CONTAINER_IP}:8000" \
	-o ExitOnForwardFailure=yes "$TARGET"
