#!/usr/bin/env bash
# Prove the deployed stack works. Run after ./deploy.sh.
#
#     ./verify.sh                    everything except a real sign-in
#     ./verify.sh --login EMAIL      also sign in for real; prompts for the
#                                    password, never takes it as an argument
#
# Exit 0 only when every check passed.
#
# The checks are grouped by what they establish, and each one prints what to do
# when it fails. Three of them matter more than the rest and are worth naming:
#
#   - the app-state database is reached over VERIFIED TLS. Anything less means
#     something inside the VPC can present its own certificate and read every
#     stored credential.
#   - the analyzer is NOT reachable from the host or the internet. It executes
#     caller-supplied SQL against customer production databases and serves an
#     interactive console for doing so. The dashboard is the only thing that
#     may talk to it.
#   - large responses reach the browser compressed. A 25,000-row result is
#     2.87 MB raw, and the whole polling design assumes it does not go out that
#     way.
#
# A password given with --login is read with `read -s`: never an argument (ps
# shows those to every user on the box), never an environment variable, never
# echoed, and never written to a file.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

LOGIN_EMAIL=""
while [[ $# -gt 0 ]]; do
	case "$1" in
		--login) LOGIN_EMAIL="${2:?--login needs an email address}"; shift 2 ;;
		-h|--help) sed -n '2,30p' "$0"; exit 0 ;;
		*) die "Unknown option: $1" ;;
	esac
done

[[ -f "$ENV_FILE" ]] || die "deploy/.env.prod does not exist. Run preflight.sh."

HOST="$(env_value SWITCHBOARD_PUBLIC_HOST || echo localhost)"
# Non-default ports exist so the rehearsal overlay can run without root, which
# is what binding 80 and 443 needs. Production leaves both unset.
HTTPS_PORT="${SWITCHBOARD_HTTPS_PORT:-$(env_value SWITCHBOARD_HTTPS_PORT || echo 443)}"
HTTP_PORT="${SWITCHBOARD_HTTP_PORT:-$(env_value SWITCHBOARD_HTTP_PORT || echo 80)}"
HTTPS_PORT="${HTTPS_PORT:-443}"; HTTP_PORT="${HTTP_PORT:-80}"

# Written as if/else rather than `$( [[ test ]] && printf ... )`.
#
# That shorter form is a trap under `set -e`: when the port IS the default the
# test is false, so the command substitution exits non-zero, so the assignment
# does - and the script dies silently on its own second line, printing nothing
# at all. It failed exactly that way in production, and could not fail that way
# in rehearsal, which runs on 8443/8080 and therefore always took the true
# branch. The one configuration never exercised locally was the default one.
if [[ "$HTTPS_PORT" == "443" ]]; then
	BASE="https://${HOST}"
else
	BASE="https://${HOST}:${HTTPS_PORT}"
fi
if [[ "$HTTP_PORT" == "80" ]]; then
	HTTP_BASE="http://${HOST}"
else
	HTTP_BASE="http://${HOST}:${HTTP_PORT}"
fi

printf '%sSwitchboard verification%s  %s\n' "$C_BOLD" "$C_RESET" "$(date -u +%FT%TZ)"
info "target: $BASE"

# -k throughout: the certificate is Caddy's own, and refusing it would fail
# every check for the one reason we already know about. Section 5 verifies the
# certificate separately, which is where that property is actually tested.
CURL=(curl -sS -k --max-time 30)

# ===========================================================================
section "1. Containers"
# ===========================================================================

for svc in analyzer dashboard caddy; do
	cid="$(compose ps -q "$svc" 2>/dev/null || true)"
	if [[ -z "$cid" ]]; then
		fail "$svc has no container" "Run ./deploy.sh"
		continue
	fi
	state="$(docker inspect -f '{{.State.Status}}' "$cid")"
	health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid")"
	restarts="$(docker inspect -f '{{.RestartCount}}' "$cid")"

	if [[ "$state" != "running" ]]; then
		fail "$svc is $state, not running" "docker compose -f docker-compose.prod.yml logs --tail 50 $svc"
	elif [[ "$health" == "unhealthy" ]]; then
		fail "$svc is running but unhealthy" "docker compose -f docker-compose.prod.yml logs --tail 50 $svc"
	else
		pass "$svc is running${health:+ (health: $health)}"
	fi

	if [[ "${restarts:-0}" -gt 3 ]]; then
		# A container that keeps dying and coming back looks healthy at any
		# single moment. The restart count is the only thing that shows it.
		warn "$svc has restarted $restarts times" \
			"It is crash-looping on something. docker compose -f docker-compose.prod.yml logs $svc"
	fi
done

# ===========================================================================
section "2. App-state database (RDS)"
# ===========================================================================
#
# Run inside the analyzer container. That is not a convenience: RDS is on a
# private subnet, so the container is the only place the answer is even
# knowable, and it is the exact context the service connects from.

DB_REPORT="$(compose exec -T analyzer python - <<'PY' 2>&1 || true
import json, os, re, socket, ssl, sys, time
from urllib.parse import urlparse, parse_qs

out = {}
dsn = os.environ.get("DATABASE_URL", "")
if not dsn:
    print(json.dumps({"error": "DATABASE_URL is not set in the container"})); sys.exit()

p = urlparse(dsn)
q = parse_qs(p.query)
out["host"] = p.hostname
out["port"] = p.port or 5432
out["sslmode"] = (q.get("sslmode") or ["<unset>"])[0]
out["sslrootcert"] = (q.get("sslrootcert") or ["<unset>"])[0]

if out["sslrootcert"] != "<unset>":
    out["sslrootcert_exists"] = os.path.exists(out["sslrootcert"])
    if out["sslrootcert_exists"]:
        with open(out["sslrootcert"], "rb") as fh:
            out["sslrootcert_certs"] = fh.read().count(b"BEGIN CERTIFICATE")

try:
    infos = socket.getaddrinfo(p.hostname, out["port"], proto=socket.IPPROTO_TCP)
    out["resolved"] = sorted({i[4][0] for i in infos})
except Exception as e:
    out["dns_error"] = str(e)

try:
    import psycopg
    t = time.time()
    with psycopg.connect(dsn, connect_timeout=15) as c:
        out["connect_ms"] = round((time.time() - t) * 1000)
        row = c.execute(
            "select ssl, version, cipher from pg_stat_ssl where pid = pg_backend_pid()"
        ).fetchone()
        out["tls"] = bool(row[0]); out["tls_version"] = row[1]; out["tls_cipher"] = row[2]
        out["server_version"] = c.execute("show server_version").fetchone()[0]
        out["current_user"] = c.execute("select current_user").fetchone()[0]
        out["password_encryption"] = c.execute("show password_encryption").fetchone()[0]
        tables = [r[0] for r in c.execute(
            "select tablename from pg_tables where schemaname='public'"
        ).fetchall()]
        out["table_count"] = len(tables)
        out["has_users_table"] = "users" in tables
        if "alembic_version" in tables:
            out["alembic_head"] = c.execute(
                "select version_num from alembic_version"
            ).fetchone()[0]
        if "users" in tables:
            out["user_count"] = c.execute("select count(*) from users").fetchone()[0]
            out["admin_count"] = c.execute(
                "select count(*) from users where role = 'admin'"
            ).fetchone()[0]
except Exception as e:
    # The message can carry the DSN, and the DSN carries the password.
    out["connect_error"] = re.sub(r"(://[^:/@]+):[^@]*@", r"\1:***@", f"{type(e).__name__}: {e}")[:400]

print(json.dumps(out))
PY
)"

DB_JSON="$(printf '%s' "$DB_REPORT" | grep -o '^{.*}$' | tail -1 || true)"
jq_get() { printf '%s' "$DB_JSON" | python3 -c "import json,sys;d=json.load(sys.stdin);v=d.get('$1');print('' if v is None else v)" 2>/dev/null || true; }

if [[ -z "$DB_JSON" ]]; then
	fail "could not run the database probe inside the analyzer container" \
		"Raw output: $(printf '%s' "$DB_REPORT" | tail -3 | tr '\n' ' ')"
else
	RESOLVED="$(jq_get resolved)"
	CONNECT_ERR="$(jq_get connect_error)"

	if [[ -n "$RESOLVED" ]]; then
		pass "the database resolves from inside the container ($RESOLVED)"
		if printf '%s' "$RESOLVED" | grep -qE '(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)'; then
			pass "it is a private VPC address, not reachable from the internet"
		else
			warn "the database resolves to a public address ($RESOLVED)" \
				"Set Public access to No in the RDS console, and allow 5432 only from this instance's security group."
		fi
	else
		fail "the database name does not resolve from inside the container" \
			"$(jq_get dns_error). Check the RDS endpoint in .env.prod and that this instance is in the same VPC."
	fi

	if [[ -n "$CONNECT_ERR" ]]; then
		fail "cannot connect to the database" "$CONNECT_ERR"
		case "$CONNECT_ERR" in
			*"channel binding"*)
				info "  ^ this is channel_binding=require against an md5 instance. Remove that parameter from DATABASE_URL." ;;
			*"root certificate"*|*"certificate verify failed"*|*"self-signed"*)
				info "  ^ verify-full cannot find or trust the CA. Confirm sslrootcert=/app/certs/trust-bundle.pem" ;;
			*timeout*|*"Network is unreachable"*)
				info "  ^ this is a security group, not a credential. Allow 5432 on the RDS security group from THIS instance's security group." ;;
			*password*|*authentication*)
				info "  ^ credentials. Check the user and password in .env.prod." ;;
		esac
	else
		pass "connected in $(jq_get connect_ms) ms as $(jq_get current_user) to Postgres $(jq_get server_version)"

		if [[ "$(jq_get tls)" == "True" ]]; then
			pass "the connection is encrypted ($(jq_get tls_version), $(jq_get tls_cipher))"
		else
			fail "the connection to the database is NOT encrypted" \
				"Every stored credential crosses the VPC in clear text. Set sslmode=verify-full in DATABASE_URL."
		fi

		case "$(jq_get sslmode)" in
			verify-full) pass "sslmode=verify-full: the server's identity is verified, not just the channel" ;;
			verify-ca)   warn "sslmode=verify-ca does not check the hostname" "verify-full is strictly better and costs one word." ;;
			*)           fail "sslmode is $(jq_get sslmode), so the server is not verified" \
				"Encrypted but unauthenticated: anything answering on that address can impersonate the database. Use sslmode=verify-full&sslrootcert=/app/certs/trust-bundle.pem" ;;
		esac

		if [[ "$(jq_get sslrootcert_exists)" == "True" ]]; then
			pass "the CA bundle exists in the container with $(jq_get sslrootcert_certs) certificates"
		elif [[ "$(jq_get sslrootcert)" != "<unset>" ]]; then
			fail "sslrootcert points at $(jq_get sslrootcert), which does not exist in the container" \
				"That must be a path inside the image. Use /app/certs/trust-bundle.pem"
		fi

		if [[ "$(jq_get password_encryption)" == "md5" ]]; then
			warn "the database still uses md5 password encryption" \
				"scram-sha-256 is the modern default. Change the parameter group, then reset the password so it is re-hashed. Until then, channel_binding=require in DATABASE_URL will fail."
		else
			pass "password encryption is $(jq_get password_encryption)"
		fi

		if [[ "$(jq_get has_users_table)" == "True" ]]; then
			pass "the schema is migrated: $(jq_get table_count) tables, alembic at $(jq_get alembic_head)"
		else
			fail "no users table - migrations have not run" \
				"Check the analyzer's startup log for an Alembic error: docker compose -f docker-compose.prod.yml logs analyzer | head -50"
		fi

		ADMINS="$(jq_get admin_count)"
		if [[ -z "$ADMINS" ]]; then
			: # already reported by the users-table check above
		elif [[ "$ADMINS" == "0" ]]; then
			warn "there are no administrator accounts yet ($(jq_get user_count) users total)" \
				"Nobody can sign in. Create the first one: docker compose --env-file .env.prod -f docker-compose.prod.yml exec analyzer fae create-admin"
		else
			pass "$ADMINS administrator account(s), $(jq_get user_count) users total"
		fi
	fi
fi

# Compare the running schema against the migrations in the image, rather than
# trusting that "it started" means "it migrated". Drift here is silent and
# shows up later as a 500 on one endpoint.
HEAD_IN_IMAGE="$(compose exec -T analyzer sh -c 'cd /app && alembic heads 2>/dev/null | head -1' 2>/dev/null </dev/null | awk '{print $1}' | tr -d '\r' || true)"
HEAD_IN_DB="$(jq_get alembic_head)"
if [[ -n "$HEAD_IN_IMAGE" && -n "$HEAD_IN_DB" ]]; then
	if [[ "$HEAD_IN_IMAGE" == "$HEAD_IN_DB" ]]; then
		pass "the database is at the image's migration head ($HEAD_IN_DB)"
	else
		fail "schema drift: the database is at $HEAD_IN_DB, the image expects $HEAD_IN_IMAGE" \
			"Run the migration: docker compose --env-file .env.prod -f docker-compose.prod.yml exec analyzer alembic upgrade head"
	fi
fi

# ===========================================================================
section "3. The analyzer is not reachable from anywhere it should not be"
# ===========================================================================
#
# The most important section here. Everything else being correct does not
# matter if this is wrong.

# An unpublished port is reported differently by every compose version that has
# ever been asked: "" on some, ":0" on others, "0.0.0.0:0", and on Docker 29
# "invalidIP:0". They all mean the same thing, and the port number is the part
# that carries the meaning - so match on that rather than trying to enumerate
# the spellings, which is how this check produced a false FAIL on a correctly
# configured production host.
PUBLISHED="$(compose port analyzer 8000 2>/dev/null </dev/null || true)"
PUBLISHED="$(printf '%s' "$PUBLISHED" | tr -d '[:space:]')"
if [[ -z "$PUBLISHED" || "$PUBLISHED" == *:0 ]]; then
	pass "the analyzer publishes no host port"
else
	fail "the analyzer is published on the host at $PUBLISHED" \
		"It has an interactive SQL console and executes caller-supplied SQL. Remove the 'ports:' entry from the analyzer service in docker-compose.prod.yml."
fi

# Second, independent question: is anything at all answering on 8000? The
# check above reads compose's own view; this one reads the host. They can
# disagree - a container from a different project, or a process outside Docker
# entirely, publishes that port and compose knows nothing about it.
#
# Which of those it is decides the severity, and conflating them is how a
# developer's local stack gets reported as a production hole. If THIS stack
# publishes nothing (the authoritative check above) then whatever answers is
# something else's, which is worth saying but is not this deployment leaking.
if "${CURL[@]}" --max-time 5 -o /dev/null "http://127.0.0.1:8000/health" 2>/dev/null; then
	OWNER="$(docker ps --format '{{.Names}}\t{{.Ports}}' 2>/dev/null | grep ':8000->' | cut -f1 | tr '\n' ' ' || true)"
	if [[ -z "$PUBLISHED" || "$PUBLISHED" == ":0" || "$PUBLISHED" == "0.0.0.0:0" ]]; then
		warn "something else answers on 127.0.0.1:8000${OWNER:+ (}${OWNER}${OWNER:+)}" \
			"Not this stack - its analyzer publishes no port, checked above. Expected on a development machine running another copy; on the EC2 instance, find out what it is: sudo ss -ltnp 'sport = :8000'"
	else
		fail "this stack's analyzer answers on the host at 127.0.0.1:8000" \
			"It has an interactive SQL console and executes caller-supplied SQL. Remove the 'ports:' entry from the analyzer service in docker-compose.prod.yml."
	fi
else
	pass "nothing answers on the host at 127.0.0.1:8000"
fi

for path in /docs /openapi.json /redoc; do
	CODE="$("${CURL[@]}" -o /tmp/switchboard-probe.out -w '%{http_code}' "${BASE}${path}" 2>/dev/null || echo 000)"
	if [[ "$CODE" == "200" ]] && grep -qiE 'swagger|openapi|redoc' /tmp/switchboard-probe.out 2>/dev/null; then
		fail "the engine's API console is public at ${BASE}${path}" \
			"Caddy should proxy only to the dashboard. Check the reverse_proxy target in deploy/Caddyfile."
	else
		pass "${path} does not serve the engine console (HTTP $CODE from the dashboard)"
	fi
done
rm -f /tmp/switchboard-probe.out

# ===========================================================================
section "4. Service endpoints"
# ===========================================================================

# `</dev/null` on every exec below: `compose exec -T` inherits this script's
# stdin and reads it to EOF. When a password is piped in - which is how
# rehearse.sh drives the sign-in check - these probes swallowed it, `read` in
# section 7 then hit EOF, and `set -e` ended the run mid-prompt.
ENGINE_HEALTH="$(compose exec -T dashboard node -e \
	"fetch(process.env.ENGINE_BASE_URL+'/health').then(async r=>console.log(r.status,(await r.text()).slice(0,60))).catch(e=>console.log('ERR',e.message))" 2>/dev/null </dev/null || true)"
if [[ "$ENGINE_HEALTH" == 200* ]]; then
	pass "the dashboard can reach the engine's /health over the compose network"
else
	fail "the dashboard cannot reach the engine (got: ${ENGINE_HEALTH:-no answer})" \
		"ENGINE_BASE_URL must be http://analyzer:8000 - the service name, not localhost."
fi

ENGINE_READY="$(compose exec -T analyzer python -c \
	"import urllib.request;r=urllib.request.urlopen('http://127.0.0.1:8000/ready',timeout=8);print(r.status)" 2>/dev/null </dev/null || echo "ERR")"
if [[ "$ENGINE_READY" == "200" ]]; then
	pass "the engine's /ready passes, so it can reach its own database"
else
	fail "the engine's /ready does not pass (got: $ENGINE_READY)" \
		"It cannot reach RDS. Section 2 above says why."
fi

CODE="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "${BASE}/healthz")"
[[ "$CODE" == "200" ]] && pass "/healthz answers 200 through the proxy" \
	|| fail "/healthz answered $CODE" "The dashboard container is not serving. Check its log."

READY_BODY="$("${CURL[@]}" "${BASE}/readyz")"
if printf '%s' "$READY_BODY" | grep -q '"ready"'; then
	pass "/readyz reports ready end to end"
else
	fail "/readyz is not ready: $READY_BODY" "The dashboard is up but cannot reach the engine."
fi

if printf '%s' "$READY_BODY" | grep -qE 'analyzer:8000|http://'; then
	fail "/readyz leaks the engine's address in its body" \
		"That address is meant to stay off the public internet. See src/app/readyz/route.ts."
else
	pass "/readyz does not disclose the engine's address"
fi

# ===========================================================================
section "5. TLS and the front door"
# ===========================================================================

REDIRECT="$(curl -sS -k -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 10 "${HTTP_BASE}/" 2>/dev/null || echo "000")"
case "$REDIRECT" in
	30[178]*https://*)
		pass "plain HTTP redirects to HTTPS (${REDIRECT})" ;;
	200*)
		fail "plain HTTP serves the app instead of redirecting" \
			"The session cookie is Secure, so a browser will not store it over HTTP: sign-in silently fails and returns to the login page with no error. Check the :80 block in deploy/Caddyfile." ;;
	*)
		warn "unexpected answer on plain HTTP: $REDIRECT" \
			"If this times out, the security group is not allowing inbound 80." ;;
esac

CERT="$(echo | timeout 10 openssl s_client -connect "${HOST}:${HTTPS_PORT}" -servername "${HOST}" 2>/dev/null | openssl x509 -noout -subject -issuer -dates 2>/dev/null || true)"
if [[ -n "$CERT" ]]; then
	pass "TLS negotiates and the server presents a certificate"
	printf '%s\n' "$CERT" | sed 's/^/        /'
	if printf '%s' "$CERT" | grep -qi "Caddy Local Authority"; then
		info "self-signed by Caddy's local CA - expected with no domain name. Browsers will warn."
		info "to remove the warning: point a domain here, set SWITCHBOARD_PUBLIC_HOST to it, and delete 'tls internal' from deploy/Caddyfile"
	else
		pass "the certificate is not from Caddy's local CA, so it is publicly trusted"
	fi
else
	fail "no TLS handshake on ${HOST}:${HTTPS_PORT}" \
		"Either the security group blocks 443 or caddy is not running. docker compose -f docker-compose.prod.yml logs caddy"
fi

HEADERS="$("${CURL[@]}" -o /dev/null -D - "${BASE}/login" 2>/dev/null || true)"
check_header() {
	local name="$1" expect="$2" remedy="$3"
	local got
	got="$(printf '%s' "$HEADERS" | grep -i "^${name}:" | head -1 | cut -d: -f2- | tr -d '\r' | sed 's/^ *//')"
	if [[ -z "$got" ]]; then
		fail "$name is missing" "$remedy"
	elif [[ -n "$expect" && "$got" != *"$expect"* ]]; then
		warn "$name is '$got', expected to contain '$expect'" "$remedy"
	else
		pass "$name: $got"
	fi
}
check_header "strict-transport-security" "max-age" "Add it to the header block in deploy/Caddyfile."
check_header "x-frame-options" "DENY" "Add it to the header block in deploy/Caddyfile."
check_header "x-content-type-options" "nosniff" "Add it to the header block in deploy/Caddyfile."
check_header "referrer-policy" "" "Add it to the header block in deploy/Caddyfile."

if printf '%s' "$HEADERS" | grep -qi "^server:.*caddy"; then
	warn "the Server header names Caddy and its version" "The '-Server' line in deploy/Caddyfile should remove it."
else
	pass "the Server header does not advertise the proxy"
fi

if printf '%s' "$HEADERS" | grep -qi "^x-powered-by:"; then
	warn "X-Powered-By names the framework" "Set poweredByHeader: false in the dashboard's next.config.ts."
else
	pass "X-Powered-By is not sent"
fi

# ===========================================================================
section "6. Compression on the wire"
# ===========================================================================
#
# The polling design assumes this. A 25,000-row result is 2.87 MB of JSON that
# compresses about tenfold, and uncompressed it costs an analyst on a 20 Mbps
# link over a second of transfer per card - which no amount of query tuning
# shows up against. Between the containers it is deliberately OFF, because
# Node's fetch decodes it anyway; here it must be ON.

RAW_BYTES="$("${CURL[@]}" -o /dev/null -w '%{size_download}' -H 'Accept-Encoding: identity' "${BASE}/login" 2>/dev/null || echo 0)"
ENC_HDR="$("${CURL[@]}" -o /dev/null -D - -H 'Accept-Encoding: gzip, zstd, br' "${BASE}/login" 2>/dev/null | grep -i '^content-encoding:' | tr -d '\r' | cut -d' ' -f2- || true)"
ENC_BYTES="$("${CURL[@]}" -o /dev/null -w '%{size_download}' -H 'Accept-Encoding: gzip, zstd, br' "${BASE}/login" 2>/dev/null || echo 0)"

if [[ -n "$ENC_HDR" ]]; then
	if [[ ${RAW_BYTES:-0} -gt 0 && ${ENC_BYTES:-0} -gt 0 ]]; then
		RATIO="$(awk -v a="$RAW_BYTES" -v b="$ENC_BYTES" 'BEGIN{printf "%.1f", a/b}')"
		pass "responses are compressed with ${ENC_HDR} (${RAW_BYTES} -> ${ENC_BYTES} bytes, ${RATIO}x)"
	else
		pass "responses are compressed with ${ENC_HDR}"
	fi
else
	fail "responses reach the browser uncompressed (${RAW_BYTES} bytes either way)" \
		"A 25,000-row result goes out at 2.87 MB instead of 0.64 MB. Check the 'encode zstd gzip' line in deploy/Caddyfile."
fi

# ===========================================================================
section "7. Authentication"
# ===========================================================================

UNAUTH="$("${CURL[@]}" -o /tmp/switchboard-unauth.json -w '%{http_code}' "${BASE}/api/connections" 2>/dev/null || echo 000)"
if [[ "$UNAUTH" == "401" ]]; then
	pass "an unauthenticated API call is refused with 401"
else
	fail "an unauthenticated call to /api/connections returned $UNAUTH, not 401" \
		"$(head -c 200 /tmp/switchboard-unauth.json 2>/dev/null)"
fi
rm -f /tmp/switchboard-unauth.json

CSRF="$("${CURL[@]}" -o /dev/null -w '%{http_code}' -X POST -H 'Cookie: switchboard_session=not-a-real-token' "${BASE}/api/connections" 2>/dev/null || echo 000)"
if [[ "$CSRF" == "403" ]]; then
	pass "a mutating request without the CSRF header is refused with 403"
else
	warn "a POST without the CSRF header returned $CSRF, expected 403" \
		"See CSRF_HEADER in the dashboard's src/app/api/[...path]/route.ts."
fi

# `example.com`, not a `.test` or `.invalid` address. The engine validates the
# email before it ever looks up an account, and a non-deliverable domain is
# rejected at 422 - which passes through Caddy and the dashboard but never
# reaches authentication, so it proves far less than it appears to. Measured:
# nobody@invalid.test -> 422 REQUEST_VALIDATION_ERROR;
# no-such-user@example.com -> 401 INVALID_CREDENTIALS.
BAD_LOGIN="$("${CURL[@]}" -o /tmp/switchboard-login.json -w '%{http_code}' \
	-X POST -H 'content-type: application/json' \
	-d '{"email":"no-such-user@example.com","password":"definitely-not-the-password"}' \
	"${BASE}/api/auth/login" 2>/dev/null || echo 000)"
if [[ "$BAD_LOGIN" == "401" ]]; then
	# The whole chain in one request: Caddy -> dashboard -> analyzer -> the
	# database and back. A wrong password proves every hop without needing a
	# right one.
	pass "a wrong password is rejected with 401, which exercises every hop through to RDS"
elif [[ "$BAD_LOGIN" == "422" ]]; then
	fail "the login probe was rejected as malformed (422), not as wrong credentials" \
		"It never reached authentication, so this proved nothing about it. $(head -c 200 /tmp/switchboard-login.json 2>/dev/null)"
elif [[ "$BAD_LOGIN" == "502" || "$BAD_LOGIN" == "000" ]]; then
	fail "the login path is broken (HTTP $BAD_LOGIN)" \
		"$(head -c 200 /tmp/switchboard-login.json 2>/dev/null). The dashboard cannot reach the engine, or the engine cannot reach RDS."
else
	fail "a wrong password returned $BAD_LOGIN" "$(head -c 200 /tmp/switchboard-login.json 2>/dev/null)"
fi

if grep -q '"token"' /tmp/switchboard-login.json 2>/dev/null; then
	fail "the login response body contains a token field" \
		"The token must never reach the browser; it belongs in the httpOnly cookie only. See src/app/api/auth/login/route.ts."
fi
rm -f /tmp/switchboard-login.json

if [[ -n "$LOGIN_EMAIL" ]]; then
	printf '        password for %s (not echoed): ' "$LOGIN_EMAIL"
	# `|| true`: read returns non-zero at EOF, and under `set -e` that ends the
	# script rather than reporting anything. An empty password is handled below
	# as a failed sign-in, which is the honest outcome.
	read -rs LOGIN_PASSWORD || true
	printf '\n'
	if [[ -z "$LOGIN_PASSWORD" ]]; then
		warn "no password was given, so the sign-in check did not run" \
			"Run ./verify.sh --login $LOGIN_EMAIL from a terminal and type it at the prompt."
		LOGIN_EMAIL=""
	fi

	LOGIN_HDRS="$("${CURL[@]}" -o /tmp/switchboard-real.json -D - \
		-X POST -H 'content-type: application/json' \
		--data-binary @<(printf '{"email":"%s","password":"%s"}' "$LOGIN_EMAIL" "$LOGIN_PASSWORD") \
		"${BASE}/api/auth/login" 2>/dev/null || true)"
	unset LOGIN_PASSWORD

	if printf '%s' "$LOGIN_HDRS" | grep -q "HTTP/[0-9.]* 200"; then
		pass "signed in as $LOGIN_EMAIL"

		COOKIE_LINE="$(printf '%s' "$LOGIN_HDRS" | grep -i '^set-cookie: switchboard_session' | tr -d '\r' || true)"
		if [[ -z "$COOKIE_LINE" ]]; then
			fail "the sign-in set no session cookie" "Nobody will stay signed in. See src/lib/session.ts."
		else
			pass "a session cookie was set"
			[[ "$COOKIE_LINE" == *HttpOnly* ]] \
				&& pass "the cookie is HttpOnly, so script on the page cannot read it" \
				|| fail "the session cookie is not HttpOnly" "An XSS bug becomes account takeover. See src/lib/session.ts."
			[[ "$COOKIE_LINE" == *Secure* ]] \
				&& pass "the cookie is Secure, so it is never sent over plain HTTP" \
				|| fail "the session cookie is not Secure" "NODE_ENV must be production in the dashboard container."
			# Matched case-insensitively: Next writes the value lowercase
			# (`SameSite=lax`), and a case-sensitive test warned that a
			# correctly restricted cookie was unrestricted - a false alarm
			# about the exact control it was checking.
			if printf '%s' "$COOKIE_LINE" | grep -qiE 'samesite=(lax|strict)'; then
				pass "the cookie is SameSite-restricted"
			else
				warn "the session cookie has no SameSite attribute" \
					"A cross-site form post can ride it. See sessionCookie() in the dashboard's src/lib/session.ts."
			fi
		fi

		if grep -qi '"token"\|"password' /tmp/switchboard-real.json 2>/dev/null; then
			fail "the sign-in response body carries a token or a password field" \
				"Only the user object may be returned. See src/app/api/auth/login/route.ts."
		else
			pass "the response body carries the user only, no token"
		fi
	else
		STATUS="$(printf '%s' "$LOGIN_HDRS" | head -1 | tr -d '\r')"
		fail "sign-in failed: $STATUS" \
			"$(head -c 200 /tmp/switchboard-real.json 2>/dev/null). If the password is right, the account may be locked out - check the analyzer log."
	fi
	rm -f /tmp/switchboard-real.json
else
	info "skipping the real sign-in. To include it: ./verify.sh --login you@example.com"
fi

# ===========================================================================
section "8. Response times"
# ===========================================================================
#
# Not a benchmark. A sanity check that the front door is not seconds slow,
# which usually means the proxy is retrying something or DNS is timing out
# somewhere in the chain.

timed() {
	local label="$1" url="$2" budget_ms="$3" total best=99999
	for _ in 1 2 3; do
		total="$("${CURL[@]}" -o /dev/null -w '%{time_total}' "$url" 2>/dev/null || echo 9)"
		total="$(awk -v t="$total" 'BEGIN{printf "%d", t*1000}')"
		[[ $total -lt $best ]] && best=$total
	done
	if [[ $best -le $budget_ms ]]; then
		pass "$label: ${best} ms (budget ${budget_ms} ms)"
	else
		warn "$label: ${best} ms, over the ${budget_ms} ms budget" \
			"Slow here means slow for every request. Check the analyzer log for repeated database reconnects."
	fi
}
timed "static page through the proxy" "${BASE}/login" 800
timed "dashboard liveness" "${BASE}/healthz" 300
timed "engine reachability through two hops" "${BASE}/readyz" 1000

# ===========================================================================

summary "Verification" && cat <<EOF

  The stack is serving at ${C_BOLD}${BASE}/${C_RESET}

EOF
