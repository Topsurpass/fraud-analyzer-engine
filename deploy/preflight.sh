#!/usr/bin/env bash
# Check the configuration BEFORE anything starts.
#
#     ./preflight.sh
#
# Everything here is same-input-same-output: file modes, string shapes, port
# availability, core counts. None of it needs the stack running, and all of it
# is cheaper to find out now than after a deploy that comes up half-working.
#
# Exit 0 means deploy.sh has what it needs. It does NOT mean the deploy will
# work - the database is on a private subnet and only the containers can reach
# it, so that proof is verify.sh's job, after the stack is up.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

printf '%sSwitchboard preflight%s  %s\n' "$C_BOLD" "$C_RESET" "$(date -u +%FT%TZ)"

# --- 1. the host ------------------------------------------------------------

section "Host"

if command -v docker >/dev/null 2>&1; then
	pass "docker is installed ($(docker --version | cut -d, -f1))"
else
	fail "docker is not installed" "Run ./bootstrap-ec2.sh"
fi

if docker info >/dev/null 2>&1; then
	pass "the docker daemon is reachable by this user"
else
	fail "cannot talk to the docker daemon as $(id -un)" \
		"Either the daemon is stopped (sudo systemctl start docker) or this user is not in the docker group. bootstrap-ec2.sh adds it; the group only takes effect in a NEW login shell, so log out and back in."
fi

if docker compose version >/dev/null 2>&1; then
	pass "the compose plugin is installed ($(docker compose version --short 2>/dev/null))"
elif command -v docker-compose >/dev/null 2>&1; then
	warn "using standalone docker-compose, not the plugin" \
		"Works, but the plugin is what is maintained. bootstrap-ec2.sh installs it."
else
	fail "no docker compose" "Run ./bootstrap-ec2.sh"
fi

CORES="$(nproc 2>/dev/null || echo 1)"
MEM_MB="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)"
SWAP_MB="$(awk '/SwapTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)"
info "cpu cores: $CORES, memory: ${MEM_MB} MB, swap: ${SWAP_MB} MB"

# Swap counts. The Next build is a memory *peak*, not a sustained working set,
# and a peak is exactly what swap is for - slowly, but it completes rather than
# being killed. Judging on RAM alone told an instance with 2 GB of swap already
# configured that it could not build, which is both wrong and unactionable.
USABLE_MB=$((MEM_MB + SWAP_MB))
if [[ $MEM_MB -gt 0 && $USABLE_MB -lt 2600 ]]; then
	# The failure this prevents has no useful symptom: the kernel kills a
	# compiler process and `npm run build` exits with no message about memory.
	fail "only ${USABLE_MB} MB of memory + swap; the dashboard build needs about 2.5 GB" \
		"Add swap - ./bootstrap-ec2.sh does this, or by hand: sudo fallocate -l 3G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab. On ${MEM_MB} MB of RAM the build will be slow even then. Better: do not build here at all - see 'Build on your laptop, ship to the instance' in deploy/README.md, or just run ./ship-images.sh from your laptop."
elif [[ $MEM_MB -lt 1800 ]]; then
	warn "${MEM_MB} MB of RAM, reaching ${USABLE_MB} MB with swap" \
		"Enough to finish, but the dashboard build will swap hard and can take 15-30 minutes on ${CORES} cores. 'Build on your laptop, ship to the instance' in deploy/README.md avoids it entirely."
else
	pass "memory is enough to build the dashboard image (${USABLE_MB} MB usable)"
fi

DISK_AVAIL_GB="$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0)"
if [[ ${DISK_AVAIL_GB:-0} -lt 8 ]]; then
	fail "only ${DISK_AVAIL_GB} GB free on /" \
		"Two image builds plus node_modules and layer cache need roughly 8 GB free."
	# What is actually using it decides which remedy applies, and guessing
	# wrong wastes the operator's time: `docker system prune` reclaims nothing
	# on a fresh instance, where the answer is almost always that the root
	# volume is still the AMI's default 8 GB.
	ROOT_DEV="$(findmnt -no SOURCE / 2>/dev/null || true)"
	ROOT_SIZE="$(df -BG --output=size / 2>/dev/null | tail -1 | tr -dc '0-9' || echo '?')"
	info "root filesystem is ${ROOT_SIZE} GB on ${ROOT_DEV:-unknown}"

	# Reclaimable first: it is instant, needs no AWS, and a failed build leaves
	# layers that are usually the largest thing on the volume.
	if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
		RECLAIM="$(docker system df --format '{{.Type}}: {{.Reclaimable}}' 2>/dev/null | tr '\n' ', ' || true)"
		info "docker holds: ${RECLAIM:-nothing measurable}"
		info "try this first, it needs no AWS change:  docker system prune -af"
	fi

	# Then say WHICH resize is needed, rather than listing both and leaving the
	# reader to work it out. Running growpart when the disk has not been
	# enlarged prints "NOCHANGE: partition N ... cannot be grown", which reads
	# like growpart is broken when in fact the AWS-side step has not happened.
	if [[ -n "$ROOT_DEV" ]] && command -v lsblk >/dev/null 2>&1; then
		PART_NAME="$(basename "$ROOT_DEV")"
		DISK_NAME="$(lsblk -no PKNAME "$ROOT_DEV" 2>/dev/null | head -1 || true)"
		if [[ -n "$DISK_NAME" ]]; then
			DISK_BYTES="$(lsblk -bdno SIZE "/dev/$DISK_NAME" 2>/dev/null || echo 0)"
			PART_BYTES="$(lsblk -bdno SIZE "$ROOT_DEV" 2>/dev/null || echo 0)"
			# A gigabyte of slack: the other partitions on an Ubuntu AMI (/boot,
			# EFI) legitimately account for a little over 1 GB, so an exact
			# comparison would always claim there is room to grow.
			SLACK=$(( (DISK_BYTES - PART_BYTES) / 1024 / 1024 / 1024 ))
			info "disk /dev/${DISK_NAME} is $((DISK_BYTES / 1024 / 1024 / 1024)) GB, partition ${ROOT_DEV} is $((PART_BYTES / 1024 / 1024 / 1024)) GB"
			if [[ $SLACK -ge 2 ]]; then
				info "the disk is already bigger than the partition - grow the partition into it:"
				info "  sudo growpart /dev/${DISK_NAME} ${PART_NAME##*[!0-9]}  &&  sudo resize2fs ${ROOT_DEV}"
			else
				info "the partition already fills the disk, so growpart has nothing to grow into."
				info "enlarge the VOLUME in AWS first - growpart cannot make a disk bigger, only AWS can:"
				info "  EC2 console -> Elastic Block Store -> Volumes -> this instance's volume"
				info "  -> Actions -> Modify volume -> Size 30 -> Modify   (free-tier ceiling, no reboot)"
				info "then confirm 'lsblk /dev/${DISK_NAME}' shows the new size, and only then:"
				info "  sudo growpart /dev/${DISK_NAME} ${PART_NAME##*[!0-9]}  &&  sudo resize2fs ${ROOT_DEV}"
			fi
		fi
	fi
	info "full walkthrough: 'Not enough disk' in deploy/README.md"
else
	pass "disk space on / is sufficient (${DISK_AVAIL_GB} GB free)"
fi

# --- 2. the config file -----------------------------------------------------

section "Configuration file"

if [[ ! -f "$ENV_FILE" ]]; then
	fail "deploy/.env.prod does not exist" \
		"cp .env.prod.example .env.prod && \$EDITOR .env.prod"
	summary "Preflight" || exit 1
fi
pass "deploy/.env.prod exists"

MODE="$(stat -c '%a' "$ENV_FILE")"
if [[ "$MODE" != "600" && "$MODE" != "400" ]]; then
	fail ".env.prod is mode $MODE, readable beyond its owner" \
		"It holds the RDS password and the credential encryption key. Fix with: chmod 600 $ENV_FILE"
else
	pass ".env.prod is mode $MODE, owner-only"
fi

if git -C "$DEPLOY_DIR" ls-files --error-unmatch .env.prod >/dev/null 2>&1; then
	fail ".env.prod is TRACKED BY GIT" \
		"It contains live secrets. Remove it from the index now: git rm --cached deploy/.env.prod"
else
	pass ".env.prod is not tracked by git"
fi

# --- 3. the values ----------------------------------------------------------

section "Configuration values"

PUBLIC_HOST="$(env_value SWITCHBOARD_PUBLIC_HOST || true)"
if [[ -z "$PUBLIC_HOST" ]]; then
	fail "SWITCHBOARD_PUBLIC_HOST is empty" "Set it to this instance's public DNS name or IP."
elif [[ "$PUBLIC_HOST" == *"ec2-0-0-0-0"* || "$PUBLIC_HOST" == *"example.com"* ]]; then
	fail "SWITCHBOARD_PUBLIC_HOST is still the placeholder ($PUBLIC_HOST)" \
		"Caddy issues its certificate for this exact string, so a placeholder means every browser gets a name-mismatch warning on top of the self-signed one."
else
	pass "SWITCHBOARD_PUBLIC_HOST is set ($PUBLIC_HOST)"
fi

# Ask the instance metadata service what this box's public address actually is,
# and compare. IMDSv2, because IMDSv1 is disabled on anything created recently
# and a v1 call there hangs rather than failing. Best effort: this script must
# work when run somewhere that is not EC2 at all.
IMDS_TOKEN="$(curl -s --max-time 2 -X PUT 'http://169.254.169.254/latest/api/token' \
	-H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null || true)"
if [[ -n "$IMDS_TOKEN" ]]; then
	REAL_DNS="$(curl -s --max-time 2 -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
		http://169.254.169.254/latest/meta-data/public-hostname 2>/dev/null || true)"
	REAL_IP="$(curl -s --max-time 2 -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
		http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true)"
	if [[ -n "$REAL_DNS$REAL_IP" ]]; then
		if [[ "$PUBLIC_HOST" == "$REAL_DNS" || "$PUBLIC_HOST" == "$REAL_IP" ]]; then
			pass "SWITCHBOARD_PUBLIC_HOST matches this instance's public address"
		else
			warn "SWITCHBOARD_PUBLIC_HOST does not match this instance" \
				"Instance is ${REAL_DNS:-<no public dns>} / ${REAL_IP:-<no public ip>}. Fine if a domain or load balancer points here; wrong otherwise, and the certificate will mismatch."
		fi
	fi
else
	info "not on EC2, or IMDS unreachable - skipping the public-address cross-check"
fi

FERNET="$(env_value FAE_FERNET_KEY || true)"
if [[ -z "$FERNET" ]]; then
	fail "FAE_FERNET_KEY is empty" \
		"Every saved target-database password is encrypted with it. Generate one and paste it in: openssl rand -base64 32 | tr '+/' '-_'"
elif [[ ! "$FERNET" =~ ^[A-Za-z0-9_-]{43}=$ ]]; then
	# Fernet wants exactly 32 bytes in urlsafe base64, which is always 43
	# characters and a pad. A key of the wrong shape fails at the first
	# decrypt rather than at startup, which is a long way from the cause.
	fail "FAE_FERNET_KEY is not 32 bytes of urlsafe base64" \
		"Expected 43 urlsafe-base64 characters and one '='. Note the 'tr' - plain base64 emits + and / which Fernet rejects: openssl rand -base64 32 | tr '+/' '-_'"
else
	# The fingerprint, never the key. Comparing it across two deploys is what
	# turns "the passwords broke again" into "these are two different keys".
	FP="$(printf '%s' "$FERNET" | sha256sum | cut -c1-12)"
	pass "FAE_FERNET_KEY is well formed (fingerprint $FP)"
	info "record that fingerprint - if it ever changes, every saved credential stops decrypting"
fi

DSN="$(env_value DATABASE_URL || true)"
if [[ -z "$DSN" ]]; then
	fail "DATABASE_URL is empty" "Copy the endpoint from the RDS console; see .env.prod.example."
elif [[ "$DSN" == *"your-db.abcdefg"* || "$DSN" == *"USER:PASSWORD"* ]]; then
	fail "DATABASE_URL is still the placeholder" "Fill in the real RDS endpoint and credentials."
else
	pass "DATABASE_URL is set ($(redact_dsn "$DSN"))"

	case "$DSN" in
		postgresql://*|postgres://*) pass "DATABASE_URL is a Postgres URL" ;;
		*) fail "DATABASE_URL does not start with postgresql://" "App state is Postgres. A sqlite file on a container filesystem is silently recreated empty on every deploy." ;;
	esac

	case "$DSN" in
		*sslmode=verify-full*)
			pass "sslmode=verify-full: the connection is encrypted AND the server is verified" ;;
		*sslmode=verify-ca*)
			warn "sslmode=verify-ca verifies the CA but not the hostname" \
				"verify-full is one word longer and closes the gap. Change it unless you have a specific reason." ;;
		*sslmode=require*)
			warn "sslmode=require encrypts but verifies nothing" \
				"Anything that can answer on that address inside the VPC can present its own certificate and read every credential this database stores. Change to: sslmode=verify-full&sslrootcert=/app/certs/trust-bundle.pem" ;;
		*sslmode=*)
			fail "DATABASE_URL requests a non-verifying, possibly plaintext sslmode" \
				"Use sslmode=verify-full&sslrootcert=/app/certs/trust-bundle.pem" ;;
		*)
			fail "DATABASE_URL names no sslmode" \
				"libpq then defaults to 'prefer', which silently accepts an unencrypted connection. Append: ?sslmode=verify-full&sslrootcert=/app/certs/trust-bundle.pem" ;;
	esac

	if [[ "$DSN" == *verify-full* || "$DSN" == *verify-ca* ]]; then
		case "$DSN" in
			*sslrootcert=/app/certs/trust-bundle.pem*)
				pass "sslrootcert points at the bundle baked into the image" ;;
			*sslrootcert=*)
				CERT_PATH="$(printf '%s' "$DSN" | sed -E 's/.*sslrootcert=([^&]*).*/\1/')"
				warn "sslrootcert is $CERT_PATH, not the image's bundle" \
					"That path must exist INSIDE the analyzer container, not on this host. /app/certs/trust-bundle.pem is built by the Dockerfile from the system roots plus AWS's 108 RDS roots." ;;
			*)
				fail "a verifying sslmode with no sslrootcert" \
					"libpq then looks in ~/.postgresql/root.crt, which does not exist in the container, and every connection fails. Append: &sslrootcert=/app/certs/trust-bundle.pem" ;;
		esac
	fi

	if [[ "$DSN" == *channel_binding=require* ]]; then
		warn "channel_binding=require is carried over from a Neon URL" \
			"It fails outright on an RDS instance still using md5 password encryption, with an error that never mentions md5. verify-full already provides what this is usually reached for. verify.sh reports which auth your instance uses."
	fi

	DB_HOST="$(printf '%s' "$DSN" | sed -E 's#^[a-z+]+://[^@]*@([^:/?]+).*#\1#')"
	if [[ -n "$DB_HOST" ]]; then
		# `timeout`, because getent has none of its own: a name that goes to a
		# slow or unreachable resolver hangs it forever, and preflight then hangs
		# with it - silently, mid-run, with no output to say what it is waiting
		# for. Measured here against a name that does not resolve: still blocked
		# at ten seconds.
		RESOLVED="$(timeout 5 getent hosts "$DB_HOST" 2>/dev/null | awk '{print $1}' | head -1 || true)"
		if [[ -z "$RESOLVED" ]]; then
			warn "cannot resolve $DB_HOST from this host" \
				"Expected if DNS is VPC-internal and this is not the EC2 box. verify.sh checks it from inside the container, which is the answer that matters."
		elif [[ "$RESOLVED" =~ ^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.) ]]; then
			pass "the database resolves to a private VPC address ($RESOLVED), not the public internet"
		else
			warn "the database resolves to a PUBLIC address ($RESOLVED)" \
				"An RDS instance holding credentials should not be publicly accessible. In the RDS console set Public access to No, and allow 5432 only from this instance's security group."
		fi
	fi
fi

WORKERS="$(env_value WEB_CONCURRENCY || echo 2)"
if [[ "$WORKERS" =~ ^[0-9]+$ ]] && [[ $WORKERS -gt $((CORES * 2)) ]]; then
	warn "WEB_CONCURRENCY=$WORKERS on $CORES cores" \
		"More workers than cores does not add parallelism - a poll is GIL-bound Python - and each worker holds its own copy of both result caches. Try $CORES."
else
	pass "WEB_CONCURRENCY=$WORKERS is sane for $CORES cores"
fi

# --- 4. the other repository ------------------------------------------------

section "Dashboard repository"

DASH_CTX="$(env_value DASHBOARD_CONTEXT || echo '../../fraud-analyzer-dashboard')"
DASH_ABS="$(cd "$DEPLOY_DIR" && cd "$DASH_CTX" 2>/dev/null && pwd || true)"
if [[ -z "$DASH_ABS" ]]; then
	fail "DASHBOARD_CONTEXT does not resolve to a directory ($DASH_CTX)" \
		"Clone the dashboard beside this repository, or set DASHBOARD_CONTEXT to where it is."
elif [[ ! -f "$DASH_ABS/Dockerfile" ]]; then
	fail "no Dockerfile at $DASH_ABS" \
		"That directory is not the dashboard checkout, or it is on a branch that predates the production image."
else
	pass "dashboard checkout found at $DASH_ABS"
	if grep -q 'output: "standalone"' "$DASH_ABS/next.config.ts" 2>/dev/null; then
		pass "next.config.ts emits a standalone bundle, which the Dockerfile expects"
	else
		fail "next.config.ts does not set output: \"standalone\"" \
			"The image copies .next/standalone, which is not produced without it, and the build fails at the COPY."
	fi
fi

# --- 5. the ports -----------------------------------------------------------

section "Ports"

for port in 80 443; do
	# `ss` on a modern Ubuntu; fall back to a connect attempt where it is
	# missing. Only listeners this stack does not own are a problem, so a
	# running caddy from a previous deploy is reported as such rather than as
	# a conflict.
	if command -v ss >/dev/null 2>&1 && ss -ltnH "sport = :$port" 2>/dev/null | grep -q .; then
		if compose ps --status running 2>/dev/null | grep -q caddy; then
			pass "port $port is held by this stack's caddy (a redeploy will reuse it)"
		else
			fail "port $port is already in use by something else" \
				"Find it with: sudo ss -ltnp 'sport = :$port'. Nginx and Apache are the usual answers; stop and disable whichever it is."
		fi
	else
		pass "port $port is free"
	fi
done

info "the EC2 security group must allow inbound 80 and 443 from wherever analysts sit, and nothing else"

summary "Preflight"
