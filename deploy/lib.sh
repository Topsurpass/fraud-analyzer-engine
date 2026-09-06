#!/usr/bin/env bash
# Shared output and check plumbing for the deploy scripts. Sourced, not run.
#
# Every check goes through pass/fail/warn so a run ends with one honest
# summary line and an exit code that means something: 0 only when nothing
# failed. A script that prints a wall of green and exits 0 regardless is worse
# than no script, because it is trusted.

set -euo pipefail

# Colour only when stdout is a terminal. Piped into a file or a log shipper,
# escape codes are noise that breaks grep.
if [[ -t 1 ]]; then
	C_RESET=$'\033[0m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
	C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
else
	C_RESET=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''; C_DIM=''; C_BOLD=''
fi

FAILURES=0
WARNINGS=0
CHECKS=0

section() { printf '\n%s%s%s\n' "$C_BOLD$C_BLUE" "$1" "$C_RESET"; }

pass() {
	CHECKS=$((CHECKS + 1))
	printf '  %sPASS%s  %s\n' "$C_GREEN" "$C_RESET" "$1"
}

# A failure prints the remedy on the next line, indented. A check that says
# what is wrong without saying what to do about it just moves the problem.
fail() {
	CHECKS=$((CHECKS + 1)); FAILURES=$((FAILURES + 1))
	printf '  %sFAIL%s  %s\n' "$C_RED" "$C_RESET" "$1"
	[[ $# -gt 1 ]] && printf '        %s%s%s\n' "$C_DIM" "$2" "$C_RESET"
	return 0
}

warn() {
	CHECKS=$((CHECKS + 1)); WARNINGS=$((WARNINGS + 1))
	printf '  %sWARN%s  %s\n' "$C_YELLOW" "$C_RESET" "$1"
	[[ $# -gt 1 ]] && printf '        %s%s%s\n' "$C_DIM" "$2" "$C_RESET"
	return 0
}

info() { printf '  %s----%s  %s\n' "$C_DIM" "$C_RESET" "$1"; }

die() {
	printf '\n%sFATAL%s %s\n' "$C_RED$C_BOLD" "$C_RESET" "$1" >&2
	exit 1
}

# One summary, one exit code. Warnings do not fail the run: they are things
# worth knowing that are not wrong, and conflating the two trains people to
# ignore both.
summary() {
	local label="$1"
	printf '\n'
	if [[ $FAILURES -gt 0 ]]; then
		printf '%s%s: %d of %d checks FAILED%s' \
			"$C_RED$C_BOLD" "$label" "$FAILURES" "$CHECKS" "$C_RESET"
		[[ $WARNINGS -gt 0 ]] && printf ', %d warnings' "$WARNINGS"
		printf '\n'
		return 1
	fi
	printf '%s%s: %d checks passed%s' "$C_GREEN$C_BOLD" "$label" "$CHECKS" "$C_RESET"
	[[ $WARNINGS -gt 0 ]] && printf '%s, %d warnings%s' "$C_YELLOW" "$WARNINGS" "$C_RESET"
	printf '\n'
	return 0
}

# --- environment ------------------------------------------------------------

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Both are overridable so rehearse.sh can run the same scripts against the
# rehearsal overlay without any of them growing a special case. Production
# sets neither.
ENV_FILE="${SWITCHBOARD_ENV_FILE:-$DEPLOY_DIR/.env.prod}"
COMPOSE_FILE="$DEPLOY_DIR/docker-compose.prod.yml"

# `docker compose` (plugin) or `docker-compose` (standalone), whichever exists.
# Ubuntu's docker.io package ships neither, which is why bootstrap-ec2.sh
# installs from Docker's own repository.
compose() {
	# Read at call time, not at source time. rehearse.sh sources this file and
	# only then exports SWITCHBOARD_COMPOSE_EXTRA, so a value captured up there
	# would always be empty - which is exactly what happened: the rehearsal
	# overlay was silently ignored, no postgres container was created, and the
	# analyzer started against nothing with no error to say why.
	local files=(-f "$COMPOSE_FILE")
	[[ -n "${SWITCHBOARD_COMPOSE_EXTRA:-}" ]] && files+=(-f "$SWITCHBOARD_COMPOSE_EXTRA")
	if docker compose version >/dev/null 2>&1; then
		docker compose --env-file "$ENV_FILE" "${files[@]}" "$@"
	elif command -v docker-compose >/dev/null 2>&1; then
		docker-compose --env-file "$ENV_FILE" "${files[@]}" "$@"
	else
		die "Neither 'docker compose' nor 'docker-compose' is installed. Run ./bootstrap-ec2.sh"
	fi
}

# Read one key out of .env.prod without sourcing the file.
#
# Sourcing it would execute whatever is in there, and a password containing a
# backtick or a $( would run as a command. This reads the literal value: first
# match wins, `export ` prefix tolerated, surrounding quotes stripped, and
# nothing is interpreted.
env_value() {
	local key="$1"
	[[ -f "$ENV_FILE" ]] || return 1
	sed -n -E "s/^[[:space:]]*(export[[:space:]]+)?${key}=(.*)$/\2/p" "$ENV_FILE" \
		| head -n1 \
		| sed -E 's/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/'
}

# Redact the password out of a Postgres URL before it is printed anywhere.
# Every script here prints the DSN at some point, and none of them may print
# the credential in it.
redact_dsn() {
	printf '%s' "$1" | sed -E 's#(://[^:/@]+):[^@]*@#\1:***@#'
}
