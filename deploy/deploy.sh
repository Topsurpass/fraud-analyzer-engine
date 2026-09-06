#!/usr/bin/env bash
# Build the images and bring the stack up.
#
#     ./deploy.sh              build what changed, restart, wait for healthy
#     ./deploy.sh --no-build   restart from the images already on the host
#     ./deploy.sh --pull       rebuild from scratch, ignoring the layer cache
#
# Runs preflight.sh first and refuses to continue if it fails. Ends by waiting
# for both containers to report healthy, so an exit code of 0 means the stack
# is actually serving rather than merely started.
#
# It does not run verify.sh - that is a separate command on purpose, so a
# deploy that comes up healthy and a deploy that is provably correct stay two
# different claims.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

BUILD=1
BUILD_ARGS=()
for arg in "$@"; do
	case "$arg" in
		--no-build) BUILD=0 ;;
		--pull)     BUILD_ARGS+=(--no-cache --pull) ;;
		-h|--help)  sed -n '2,16p' "$0"; exit 0 ;;
		*)          die "Unknown option: $arg" ;;
	esac
done

printf '%sSwitchboard deploy%s  %s\n' "$C_BOLD" "$C_RESET" "$(date -u +%FT%TZ)"

# --- preflight --------------------------------------------------------------

section "Preflight"
if "$DEPLOY_DIR/preflight.sh" >/tmp/switchboard-preflight.log 2>&1; then
	pass "configuration checks passed (detail in /tmp/switchboard-preflight.log)"
else
	cat /tmp/switchboard-preflight.log
	die "Preflight failed. Nothing was built or started. Fix the failures above and run again."
fi

# --- record what is being deployed ------------------------------------------
#
# Which commit is running is the first question every incident asks, and after
# the fact there is no way to answer it. Written now, while it is knowable.

section "Version"

DASH_CTX="$(env_value DASHBOARD_CONTEXT || echo '../../fraud-analyzer-dashboard')"
DASH_ABS="$(cd "$DEPLOY_DIR" && cd "$DASH_CTX" && pwd)"
ENGINE_ABS="$(cd "$DEPLOY_DIR/.." && pwd)"

describe() {
	local dir="$1"
	local sha branch dirty
	sha="$(git -C "$dir" rev-parse --short HEAD 2>/dev/null || echo unknown)"
	branch="$(git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
	dirty=""
	git -C "$dir" diff --quiet 2>/dev/null || dirty=" +uncommitted"
	printf '%s@%s%s' "$branch" "$sha" "$dirty"
}

ENGINE_VER="$(describe "$ENGINE_ABS")"
DASH_VER="$(describe "$DASH_ABS")"
info "engine    $ENGINE_VER"
info "dashboard $DASH_VER"

[[ "$ENGINE_VER" == *"+uncommitted"* || "$DASH_VER" == *"+uncommitted"* ]] && \
	warn "deploying uncommitted changes" \
		"What is running will not match any commit, so it cannot be reproduced or rolled back to."

printf '%s\tengine=%s\tdashboard=%s\n' "$(date -u +%FT%TZ)" "$ENGINE_VER" "$DASH_VER" \
	>> "$DEPLOY_DIR/deployed.log"
pass "recorded in deploy/deployed.log"

# --- build ------------------------------------------------------------------

if [[ $BUILD -eq 1 ]]; then
	section "Build"
	info "the dashboard build is the slow half: a few minutes on a small instance"
	if compose build "${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"}"; then
		pass "both images built"
	else
		die "Build failed. If the dashboard build was killed with no error, it ran out of memory - see the swap note in bootstrap-ec2.sh."
	fi
else
	section "Build"
	info "skipped (--no-build)"
fi

# --- start ------------------------------------------------------------------

section "Start"

# `up -d` recreates only what changed. The analyzer runs Alembic at startup, so
# schema migration happens here, inside the container, against RDS - there is
# no separate migrate step to forget.
compose up -d --remove-orphans
pass "containers started"

# --- wait for healthy -------------------------------------------------------

section "Health"

# Poll the healthcheck rather than sleeping. The analyzer's probe touches RDS
# and its start_period is 60s, so a cold start against a database on a private
# subnet legitimately takes a while; a fixed sleep either lies or wastes time.
wait_healthy() {
	local service="$1" deadline=$((SECONDS + 180)) cid state
	while [[ $SECONDS -lt $deadline ]]; do
		cid="$(compose ps -q "$service" 2>/dev/null || true)"
		if [[ -n "$cid" ]]; then
			state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo unknown)"
			case "$state" in
				healthy) pass "$service is healthy"; return 0 ;;
				exited|dead)
					fail "$service exited" "Last lines of its log:"
					compose logs --tail 30 "$service" | sed 's/^/        /'
					return 1 ;;
			esac
		fi
		sleep 3
	done
	fail "$service did not become healthy within 180s" "Its recent log:"
	compose logs --tail 40 "$service" | sed 's/^/        /'
	return 1
}

wait_healthy analyzer || true
wait_healthy dashboard || true

if ! compose ps --status running 2>/dev/null | grep -q caddy; then
	fail "caddy is not running" "Check its config with: docker compose -f docker-compose.prod.yml logs caddy"
else
	pass "caddy is running"
fi

# --- done -------------------------------------------------------------------

PUBLIC_HOST="$(env_value SWITCHBOARD_PUBLIC_HOST || echo localhost)"

if summary "Deploy"; then
	cat <<EOF

  Open:   ${C_BOLD}https://${PUBLIC_HOST}/${C_RESET}
          Your browser will warn about the certificate. That is expected with
          no domain name: it is Caddy's own CA. Accept it and continue.

  Prove it works:   ./verify.sh

  If this is the first deploy, create the first administrator - there is no
  HTTP route that mints one, on purpose:

      docker compose --env-file .env.prod -f docker-compose.prod.yml \\
          exec analyzer fae create-admin

EOF
else
	cat <<EOF

  Something is not healthy. Start here:

      docker compose --env-file .env.prod -f docker-compose.prod.yml logs -f analyzer
      ./verify.sh

EOF
	exit 1
fi
