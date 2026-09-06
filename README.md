# Switchboard — Fraud Analyzer

Saved SQL against any database schema, served as chart-ready JSON, watched on a
live dashboard. All the fraud logic lives in the SQL you save; the engine knows
nothing about your schema.

Two repositories, deployed together:

| Repository | What it is |
|---|---|
| `fraud-analyzer-engine` (this one) | FastAPI service, SQL safety layer, migrations, and `deploy/` for the whole stack |
| `fraud-analyzer-dashboard` | Next.js dashboard. The only thing that talks to the engine |

```
                 internet
                    │  443 (TLS)
                    ▼
            ┌───────────────┐
            │     caddy     │  TLS, compression, security headers
            └───────┬───────┘  the only container with a published port
                    │ :3000
                    ▼
            ┌───────────────┐
            │   dashboard   │  Next.js. Holds the session cookie and
            │               │  exchanges it server-side for a bearer token
            └───────┬───────┘
                    │ :8000   (container network only — never published)
                    ▼
            ┌───────────────┐
            │   analyzer    │  FastAPI. Executes saved SQL
            └───────┬───────┘
                    │ 5432 (TLS, verify-full)
                    ▼
      ┌─────────────────────────┐        ┌──────────────────────┐
      │  app state (RDS)        │        │  target databases    │
      │  users, queries, boards │        │  the data you query  │
      └─────────────────────────┘        └──────────────────────┘
```

**The browser never talks to the analyzer.** It talks to the dashboard, which
proxies server-side. That is why the session token never reaches a browser, and
why the analyzer's interactive SQL console is not one security-group mistake
away from the internet.

---

## Contents

- [Development](#development) — running both halves on your machine
- [Deployment](#deployment) — getting it onto a server
- [Reading the API documentation](#reading-the-api-documentation) — why `/docs` is not public, and how to reach it
- [Every script, explained](#every-script-explained)
- [Repository layout](#repository-layout)

---

## Development

You need Python 3.13 with [`uv`](https://docs.astral.sh/uv/), Node 22+, and
Docker (only for the Postgres the engine stores its own state in).

### 1. The engine

```bash
cd fraud-analyzer-engine/services/analyzer

uv venv --python 3.13 && uv pip install -e ".[dev]"
cp .env.example .env                    # every value has a working default
uv run alembic upgrade head             # create the schema
uv run uvicorn app.main:app --reload    # http://127.0.0.1:8000
```

That runs against a local SQLite file (`FAE_DB_BACKEND=sqlite`), which is fine
for development and wrong for anything else — a container filesystem is wiped on
every deploy. To develop against Postgres instead:

```bash
export FAE_FERNET_KEY=$(openssl rand -base64 32 | tr '+/' '-_')
docker compose up -d                    # Postgres + the analyzer, together
```

**`FAE_FERNET_KEY` is worth understanding once.** Every target-database password
you save is encrypted with it. If it changes, every saved connection keeps every
visible field correct while every query on it fails to decrypt — a failure that
surfaces one request at a time and looks nothing like its cause. The startup log
prints the key's *fingerprint* (never the key); comparing that across two runs is
how you tell "the passwords broke" from "these are two different keys".

### 2. An administrator

There is no HTTP route that creates one, deliberately: an endpoint that mints
administrators is reachable by anything that can reach the service.

```bash
uv run fae create-admin        # prompts for email, name, password
```

### 3. The dashboard

```bash
cd fraud-analyzer-dashboard
npm install

echo 'ENGINE_BASE_URL=http://127.0.0.1:8000' > .env.local
npm run dev                    # http://localhost:3000
```

`ENGINE_BASE_URL` is deliberately **not** prefixed `NEXT_PUBLIC_`. A public
variable is inlined into the client bundle, which would publish the engine's
address to every browser and undo the whole design.

If you also build the dashboard on Vercel for previews, note that
`output: "standalone"` in `next.config.ts` is switched off there. Vercel traces
files itself and reads those traces from `.next/`; standalone moves them, and the
deploy fails on a file it cannot open after a build that looked fine. The
production image needs standalone, Vercel must not have it, and the config keys
off `VERCEL` so neither side needs configuring.

### 4. Something to look at

An empty dashboard is hard to develop against, so there is a seeder that builds
a realistic payments database, registers it with your running engine, and saves
one query per chart type:

```bash
cd fraud-analyzer-dashboard
node scripts/dev-seed.mjs --email=you@example.com --password='your-password'
node scripts/dev-seed.mjs --tick     # stream new rows, so the charts move
```

It only ever talks to the engine URL you give it and only writes the SQLite file
you point it at.

### 5. Tests

```bash
# engine
cd services/analyzer
uv run pytest -m "not integration and not slow"   # gate lane, ~70s
uv run pytest                                      # everything

# dashboard
cd fraud-analyzer-dashboard
npm test          # 848 tests
npm run typecheck
npm run lint
```

Two lanes on purpose. **Gate tests** are deterministic, local, free, and run on
every commit via the pre-commit hook. **Integration and slow tests** touch
databases or deliberately burn CPU to assert a performance bound; they run in CI.

Install the pre-commit hook once: `./scripts/install-hooks.sh` (dashboard).

---

## Deployment

Full walkthrough: **[`deploy/README.md`](deploy/README.md)**. The short version:

```bash
# on your laptop, first — proves the whole stack works before AWS is involved
cd deploy && ./rehearse.sh

# on the instance, once
./bootstrap-ec2.sh
cp .env.prod.example .env.prod && chmod 600 .env.prod && $EDITOR .env.prod

# every deploy after that
./preflight.sh && ./deploy.sh && ./verify.sh
```

**On a small instance (t2/t3.micro), build on your laptop instead.** Building
peaks at ~2.5 GB of memory and ~8 GB of disk; running takes under 400 MB. A
micro runs this comfortably and cannot build it:

```bash
./ship-images.sh ubuntu@YOUR-IP -i ~/.ssh/key.pem   # laptop
./deploy.sh --no-build && ./verify.sh               # instance
```

Three settings decide whether a deployment works, and each fails quietly rather
than loudly. `.env.prod.example` explains all three in place:

- **`DATABASE_URL`** — must carry `sslmode=verify-full` and
  `sslrootcert=/app/certs/trust-bundle.pem`. `require` encrypts but verifies
  nothing; libpq's own default silently accepts an unencrypted connection.
- **`FAE_FERNET_KEY`** — back it up somewhere that is not the instance.
- **`SWITCHBOARD_PUBLIC_HOST`** — Caddy issues its certificate for exactly this
  string.

---

## Reading the API documentation

**`/docs` is deliberately unreachable from the internet, and that is not a
misconfiguration.** `deploy/verify.sh` asserts it returns 404 from the public
address, and that assertion is protecting something specific: `/docs` is not a
reference page, it is an interactive form that composes and executes SQL against
whichever customer database a connection points at, with a Try-it-out button
beside every endpoint.

So it is not published. Reach it the way anything else private is reached — over
ssh, by someone who already has the key:

```bash
cd deploy
./api-docs.sh ubuntu@YOUR-IP -i ~/.ssh/key.pem
# then open http://localhost:8899/docs
```

That forwards a local port straight to the analyzer container. Nothing is
published on the instance, no port is opened, no configuration changes, and the
tunnel closes when you press Ctrl-C.

**Locally**, where the engine is already on your own machine, just open
<http://127.0.0.1:8000/docs>.

**Offline**, [`contracts/openapi.json`](contracts/) is the same specification,
checked in and version-controlled — CI fails if it drifts from the code. Read
that if you want the contract rather than a live instance.

---

## Every script, explained

### `deploy/` — the production stack

Run these from `fraud-analyzer-engine/deploy/`.

| Script | Runs on | What it does |
|---|---|---|
| **`bootstrap-ec2.sh`** | instance, once | Installs Docker and the compose plugin from Docker's own apt repository, adds you to the `docker` group, enables the daemon at boot, and adds swap on an instance with under 3.5 GB of RAM. Safe to re-run; every step checks before acting. Touches no firewall or security-group settings — those are yours to decide. |
| **`preflight.sh`** | instance | ~21 checks on the configuration **before anything starts**: Docker present and usable, memory *and swap* against what the build actually needs, disk, `.env.prod` mode and git status, the shape of every value in it, TLS parameters on `DATABASE_URL`, whether the database resolves to a private address, and whether ports 80/443 are free. All deterministic. `deploy.sh` refuses to continue if it fails. |
| **`deploy.sh`** | instance | Runs preflight, records which commit of each repository is being deployed to `deployed.log`, builds the images, starts the stack, then **waits for both containers to report healthy** — so exit 0 means it is serving, not merely started. `--no-build` starts from images already present; `--pull` rebuilds ignoring the cache. |
| **`verify.sh`** | instance | 38 checks proving the *running* stack works, in 8 sections: containers, the RDS connection (including that TLS is real and verified), that the analyzer is unreachable from the host and the internet, service endpoints, TLS and security headers, compression on the wire, authentication, and response times. `--login EMAIL` also signs in for real, prompting for the password so it never lands in `ps` or a file. |
| **`ship-images.sh`** | laptop | Builds both images where there are resources and streams them to the instance over ssh (`docker save \| gzip \| docker load` — no temporary tarball at either end, which matters when the instance's disk is the constraint). Checks ssh and remote disk before spending minutes building, and compares layer digests afterwards so a stale image of the same name cannot pass as success. |
| **`rehearse.sh`** | laptop | Runs the entire production stack locally against a throwaway Postgres that speaks real TLS, then runs `preflight.sh` and `verify.sh` against it. This is how the deploy scripts are tested without an AWS bill. `--clean` tears it down. |
| **`api-docs.sh`** | laptop | Opens an ssh tunnel to the analyzer's API documentation. See [above](#reading-the-api-documentation). |
| **`lib.sh`** | — | Shared output and check plumbing. Sourced by the others, never run directly. Every check goes through `pass`/`fail`/`warn` so a run ends with one honest summary and an exit code that means something. |

### `services/analyzer/` — the engine

| Command | What it does |
|---|---|
| **`fae create-admin`** | Creates the first administrator. The only way one is ever minted, and deliberately not an HTTP route. |
| **`fae reset-password`** | Issues a time-limited temporary password for an account that is locked out. |
| **`fae list-users`** | Lists accounts and roles. |
| **`fae claim-unowned`** | Assigns ownerless saved queries and dashboards to an administrator. |
| **`scripts/export_openapi.py`** | Regenerates `contracts/openapi.json`. CI fails if the checked-in copy drifts from the code, so run it after changing any endpoint. |
| **`bench/`** | Performance harnesses used to justify specific decisions, kept so the numbers can be reproduced rather than trusted: `seed.py` (25,000-row fixture), `hotpath.py`, `http_poll.py`, `gzip_levels.py` (why compression level 1, not 9), `concurrency.py`, `profile_batch.py`. |

### `fraud-analyzer-dashboard/`

| Command | What it does |
|---|---|
| **`npm run dev`** | Development server on :3000. |
| **`npm run build`** / **`start`** | Production build (standalone output) and run. |
| **`npm test`** / **`test:watch`** | Vitest — 848 tests. |
| **`npm run typecheck`** / **`lint`** | `tsc --noEmit`; ESLint. |
| **`npm run smoke`** | Real browser: does every chart type actually put marks on screen? A chart that renders an empty SVG passes unit tests and fails a user. |
| **`npm run smoke:auth`** | Real browser: sign-in, roles, and account management. |
| **`npm run smoke:dashboards`** | Real browser: is a board really server-owned, not just client-filtered? |
| **`npm run check:endpoints`** | Asserts every endpoint the engine documents is reachable from the UI — a client wrapper that exists but is never called is dead code pretending to be a feature. |
| **`npm run bench`** | Client-side render cost at 25,000 rows. |
| **`scripts/dev-seed.mjs`** | Builds a realistic payments database and registers it with a running engine. `--tick` streams new rows so charts move. Dev only. |
| **`scripts/shoot.mjs`** | Screenshots a list of routes, so the UI can actually be looked at. |
| **`scripts/install-hooks.sh`** | Installs the pre-commit hook (typecheck, lint, tests). |

---

## Repository layout

| Path | What it holds |
|---|---|
| `services/analyzer/` | The service: API, SQL guard, migrations, tests, CLI |
| `services/analyzer/certs/` | AWS RDS CA bundle, baked into the image for `verify-full` |
| `deploy/` | Production stack: compose, Caddyfile, and the scripts above |
| `contracts/` | Frozen response shapes and the generated `openapi.json` |
| `scripts/` | Repository-level tooling |
| `docs/` | Design specs and implementation plans |

Further reading: [`services/analyzer/README.md`](services/analyzer/README.md)
for the safety model and the API, [`services/analyzer/DOCKER.md`](services/analyzer/DOCKER.md)
for running the container by itself, [`deploy/README.md`](deploy/README.md) for
deployment.

**Before you connect a production database, read the read-only role section in
[`services/analyzer/README.md`](services/analyzer/README.md#use-a-read-only-database-role).**
The service blocks writes at three layers, and a read-only database role is the
control that still holds if one of those layers has a bug.
