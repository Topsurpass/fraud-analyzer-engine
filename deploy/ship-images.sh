#!/usr/bin/env bash
# Build both images HERE and send them to the instance, so the instance never
# builds anything.
#
#     ./ship-images.sh ubuntu@1.2.3.4
#     ./ship-images.sh ubuntu@1.2.3.4 -i ~/.ssh/switchboard.pem
#
# Then, on the instance:
#
#     cd fraud-analyzer-engine/deploy && ./deploy.sh --no-build && ./verify.sh
#
# ## Why this exists
#
# Building is far more expensive than running. The dashboard's Next build peaks
# around 2.5 GB of memory and wants 505 npm packages plus a layer cache on
# disk; the two images that come out of it are roughly 500 MB together, and the
# running stack sits under 400 MB of memory. A t2/t3.micro - 1 GB of RAM and an
# 8 GB root volume - can comfortably run this and cannot comfortably build it.
#
# So this moves the expensive half to a machine that has the resources, and
# leaves the instance doing what it is actually sized for.
#
# The images stream over ssh rather than landing in a file at either end:
# `docker save | gzip | ssh 'docker load'`. A temporary tarball would need
# ~500 MB of free disk on the very instance whose disk is the problem.
#
# Nothing environment-specific is baked in. The images read their configuration
# from the environment at run time - which is why the same image is correct on
# your laptop and on the instance, and why no secret passes through here.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TARGET="${1:-}"
[[ -n "$TARGET" ]] || die "Usage: ./ship-images.sh user@host [ssh options...]
Example: ./ship-images.sh ubuntu@1.2.3.4 -i ~/.ssh/switchboard.pem"
shift
SSH_OPTS=("$@")

ANALYZER_IMAGE="switchboard-analyzer:latest"
DASHBOARD_IMAGE="switchboard-dashboard:latest"

printf '%sShipping images to %s%s  %s\n' "$C_BOLD" "$TARGET" "$C_RESET" "$(date -u +%FT%TZ)"

# --- 1. can we reach it -----------------------------------------------------

section "Connection"

if ssh -o BatchMode=yes -o ConnectTimeout=10 "${SSH_OPTS[@]}" "$TARGET" true 2>/dev/null; then
	pass "ssh to $TARGET works without a password prompt"
else
	die "Cannot ssh to $TARGET non-interactively.
Add -i /path/to/key.pem, or load the key into your agent: ssh-add /path/to/key.pem"
fi

if ssh "${SSH_OPTS[@]}" "$TARGET" 'docker info >/dev/null 2>&1'; then
	pass "docker on the instance is reachable by that user"
else
	die "docker is not usable as that user on the instance.
Run ./bootstrap-ec2.sh there first, then log out and back in so the docker group takes effect."
fi

# `docker load` writes the layers to the instance's disk, so it needs room -
# far less than a build, but not nothing. Checked before spending minutes
# building images that cannot land.
REMOTE_FREE_GB="$(ssh "${SSH_OPTS[@]}" "$TARGET" "df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9'" || echo 0)"
if [[ ${REMOTE_FREE_GB:-0} -lt 3 ]]; then
	die "Only ${REMOTE_FREE_GB} GB free on the instance; loading these images needs about 2 GB.
Reclaim with:  ssh $TARGET 'docker system prune -af'
Or grow the root volume - see 'Not enough disk' in deploy/README.md."
fi
pass "instance has ${REMOTE_FREE_GB} GB free, enough to load the images"

# --- 2. build here ----------------------------------------------------------

section "Build"

DASH_CTX="$(env_value DASHBOARD_CONTEXT 2>/dev/null || echo '../../fraud-analyzer-dashboard')"
[[ -n "$DASH_CTX" ]] || DASH_CTX='../../fraud-analyzer-dashboard'
DASH_ABS="$(cd "$DEPLOY_DIR" && cd "$DASH_CTX" 2>/dev/null && pwd || true)"
[[ -n "$DASH_ABS" ]] || die "Cannot find the dashboard checkout at $DASH_CTX"

# `docker build` directly rather than `docker compose build`: compose
# interpolates the whole file before doing anything, so it would demand
# DATABASE_URL and FAE_FERNET_KEY be set on this machine just to build. Nothing
# about building needs them, and a build step that asks for production secrets
# is a build step people paste production secrets into.
info "analyzer  <- $DEPLOY_DIR/../services/analyzer"
docker build -t "$ANALYZER_IMAGE" "$DEPLOY_DIR/../services/analyzer" \
	|| die "Analyzer build failed."
pass "built $ANALYZER_IMAGE"

info "dashboard <- $DASH_ABS"
docker build -t "$DASHBOARD_IMAGE" "$DASH_ABS" \
	|| die "Dashboard build failed. If it was killed with no error, this machine ran out of memory too."
pass "built $DASHBOARD_IMAGE"

SIZE="$(docker image inspect "$ANALYZER_IMAGE" "$DASHBOARD_IMAGE" \
	--format '{{.Size}}' 2>/dev/null | awk '{t+=$1} END {printf "%.0f", t/1024/1024}')"
info "uncompressed total: ${SIZE:-?} MB (less on the wire; it is gzipped in transit)"

# --- 3. ship ----------------------------------------------------------------

section "Transfer"
info "streaming over ssh - no temporary tarball at either end"
info "this is the slow part; a few minutes on a normal connection"

START=$SECONDS
# gzip -1: these layers are mostly already-compressed package data, so the
# higher levels buy very little and cost real time on a link this is not
# saturating anyway.
if docker save "$ANALYZER_IMAGE" "$DASHBOARD_IMAGE" \
	| gzip -1 \
	| ssh "${SSH_OPTS[@]}" "$TARGET" 'gunzip | docker load'; then
	pass "images loaded on the instance in $((SECONDS - START))s"
else
	die "Transfer failed. If it stopped partway, nothing on the instance was changed:
docker load applies an image only once its stream completes."
fi

# --- 4. prove they arrived --------------------------------------------------

section "Confirm"

for image in "$ANALYZER_IMAGE" "$DASHBOARD_IMAGE"; do
	LOCAL_ID="$(docker image inspect "$image" --format '{{.Id}}' 2>/dev/null || echo local-unknown)"
	REMOTE_ID="$(ssh "${SSH_OPTS[@]}" "$TARGET" "docker image inspect '$image' --format '{{.Id}}' 2>/dev/null" || echo remote-missing)"
	if [[ "$LOCAL_ID" == "$REMOTE_ID" ]]; then
		pass "$image is on the instance, same image id"
	else
		# Comparing ids, not just presence: an older image of the same name
		# sitting there would otherwise read as success, and the instance would
		# quietly keep running last week's build.
		fail "$image on the instance does not match what was built here" \
			"local ${LOCAL_ID:0:19}, remote ${REMOTE_ID:0:19}. Run this script again."
	fi
done

summary "Ship" && cat <<EOF

  Now, on the instance:

      cd fraud-analyzer-engine/deploy
      ./deploy.sh --no-build
      ./verify.sh

  --no-build is what makes this worth doing: compose finds the images by the
  names above and starts them without building anything.

EOF
