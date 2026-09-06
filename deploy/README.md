# Deploying Switchboard to EC2

Production deployment for a single Ubuntu EC2 instance with app state in RDS
Postgres.

```
                 internet
                    │
                    │ 443 (TLS)
                    ▼
            ┌───────────────┐
            │     caddy     │   TLS, compression, security headers
            └───────┬───────┘   the only container with a published port
                    │ :3000
                    ▼
            ┌───────────────┐
            │   dashboard   │   Next.js. Holds the session cookie, exchanges
            │   (Next.js)   │   it server-side for a bearer token.
            └───────┬───────┘
                    │ :8000   (compose network only)
                    ▼
            ┌───────────────┐
            │   analyzer    │   FastAPI. Executes saved SQL. No published
            │   (FastAPI)   │   port, ever.
            └───────┬───────┘
                    │ 5432 (TLS, verify-full)
                    ▼
            ┌───────────────┐
            │  RDS Postgres │   private subnet, no public access
            └───────────────┘
```

The browser never talks to the analyzer. It talks to the dashboard, which
proxies server-side. That is why the session token never reaches a browser, and
why the analyzer's `/docs` console — an interactive form that runs SQL against
customer production databases — is not one security-group mistake away from the
internet.

---

## Files

| File | What it is |
|---|---|
| `bootstrap-ec2.sh` | One-time host setup. Installs Docker, adds swap on a small instance. |
| `.env.prod.example` | Every setting, with why it matters. Copy to `.env.prod`. |
| `preflight.sh` | Checks the configuration before anything starts. |
| `deploy.sh` | Builds, starts, waits for healthy. |
| `verify.sh` | Proves the running stack actually works. 8 sections, ~45 checks. |
| `rehearse.sh` | Runs the whole thing locally against a throwaway Postgres. |
| `ship-images.sh` | Builds both images elsewhere and sends them over ssh, so a small instance never builds. |
| `docker-compose.prod.yml` | The three services. |
| `docker-compose.rehearsal.yml` | Overlay adding a local TLS Postgres, for `rehearse.sh`. |
| `Caddyfile` | TLS, compression, security headers. |
| `deployed.log` | Appended by `deploy.sh`: what commit was deployed, and when. |

---

## First deploy

### 1. On your laptop, rehearse it

Nothing here needs AWS. It builds both images, runs a real Postgres with TLS,
migrates against it, and runs `verify.sh` end to end.

```bash
cd fraud-analyzer-engine/deploy
./rehearse.sh
```

If that passes, the same scripts run unchanged on the instance. Tear it down
with `./rehearse.sh --clean`.

### 2. On the instance, set up the host

Both repositories need to be on the box, side by side:

```bash
git clone <engine-repo>    fraud-analyzer-engine
git clone <dashboard-repo> fraud-analyzer-dashboard
cd fraud-analyzer-engine/deploy
./bootstrap-ec2.sh
```

`bootstrap-ec2.sh` adds you to the `docker` group. Group membership is read at
login, so **log out and back in** before the next step.

### 3. Configure

```bash
cp .env.prod.example .env.prod
chmod 600 .env.prod
nano .env.prod
```

Three values have to be right. `.env.prod.example` explains each in place.

- **`SWITCHBOARD_PUBLIC_HOST`** — the EC2 public DNS name. Caddy issues its
  certificate for exactly this string.
- **`DATABASE_URL`** — the RDS endpoint, ending
  `?sslmode=verify-full&sslrootcert=/app/certs/trust-bundle.pem`. Both
  parameters matter; see [TLS to RDS](#tls-to-rds) below.
- **`FAE_FERNET_KEY`** — `openssl rand -base64 32 | tr '+/' '-_'`. **Back this
  up somewhere that is not this instance.** Every saved target-database
  password is encrypted with it, and there is no recovery.

### 4. Deploy

```bash
./preflight.sh   # refuses to continue on a bad config
./deploy.sh
./verify.sh
```

### 5. Create the first administrator

There is no HTTP route that mints an administrator, deliberately — an endpoint
that does is reachable by anything that can reach the service. The command is
reachable by somebody who can already read the database, which is the right
bar.

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml \
    exec analyzer fae create-admin
```

It prompts for the email, the full name, and the password twice.

### 6. AWS console

Two security groups, and they are not interchangeable.

**The instance's security group** — inbound:

| Port | Source | Why |
|---|---|---|
| 443 | wherever analysts sit | the app |
| 80 | wherever analysts sit | redirects to 443, nothing else |
| 22 | your IP only | ssh. Not `0.0.0.0/0`. |

**The RDS security group** — inbound 5432 from **the instance's security
group**, and from nothing else. Not from an IP range, not from `0.0.0.0/0`. In
the RDS console, **Public accessibility must be No**. `verify.sh` checks that
the database resolves to a private address and says so if it does not.

---

## Redeploying

```bash
git -C ~/fraud-analyzer-engine pull
git -C ~/fraud-analyzer-dashboard pull
cd ~/fraud-analyzer-engine/deploy
./deploy.sh && ./verify.sh
```

`deploy.sh` rebuilds only what changed and appends the deployed commits to
`deployed.log`. Migrations run automatically when the analyzer starts.

Options: `--no-build` restarts from the images already on the host; `--pull`
rebuilds from scratch, ignoring the layer cache.

---

## Sizing the instance

Building is far more expensive than running, and the gap is what catches
people out:

| | Building | Running |
|---|---|---|
| Memory | ~2.5 GB peak (the Next build) | under 400 MB |
| Disk | ~8 GB (node_modules, layer cache) | ~1 GB (two images) |

A t2/t3.micro — 1 GB of RAM, 8 GB root volume — runs this comfortably and
cannot build it. Two ways forward, and the second is usually better.

### Not enough disk

A fresh Ubuntu AMI gives you an 8 GB root volume, of which the OS already uses
most. `preflight.sh` prints what Docker is holding; if that is small, the
volume is simply too small and pruning reclaims nothing.

Grow it — 30 GB is the free-tier ceiling, so this costs nothing:

1. EC2 console → **Volumes** → select the instance's root volume → **Modify** →
   30 GiB → Modify. Takes a minute, no reboot, no downtime.
2. On the instance, grow the partition and the filesystem to match:

```bash
lsblk                                  # find the root device, e.g. nvme0n1p1
sudo growpart /dev/nvme0n1 1
sudo resize2fs /dev/nvme0n1p1
df -h /                                # confirm
```

The two-step is not optional: resizing the EBS volume in AWS does not resize
the partition on it, and nothing warns you that it did not.

### Not enough memory, or: building somewhere else

Add swap first — `bootstrap-ec2.sh` does it automatically under 3.5 GB of RAM,
or by hand:

```bash
sudo fallocate -l 3G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

That is enough to *finish*, but on 1 GB of RAM and 2 cores the dashboard build
swaps hard and can take 15–30 minutes. **The better answer is not to build
there at all.** Build on your laptop and ship the images:

```bash
# on your machine
cd fraud-analyzer-engine/deploy
./ship-images.sh ubuntu@1.2.3.4 -i ~/.ssh/your-key.pem

# then on the instance
cd fraud-analyzer-engine/deploy
./deploy.sh --no-build && ./verify.sh
```

`ship-images.sh` builds both images locally, streams them over ssh
(`docker save | gzip | docker load` — no temporary tarball on either side, which
matters when the instance's disk is the constraint), and compares image ids
afterwards so an older image of the same name cannot pass as success. About
550 MB uncompressed, less on the wire.

`--no-build` then starts them by name without building anything. Nothing
environment-specific is baked into either image — they read their configuration
from the environment at run time — so the image you tested locally is the image
that runs.

This also makes redeploys much faster, and keeps the instance sized for what it
actually serves rather than for its worst five minutes.

---

## TLS to RDS

Two parameters on `DATABASE_URL`, and neither is decoration.

`sslmode=verify-full` encrypts **and** checks that the server presenting the
certificate is the host you asked for. `sslmode=require` — what most
copy-pasted URLs carry — encrypts but verifies nothing, so anything that can
answer on that address inside the VPC can present its own certificate and read
every credential this database stores.

`sslrootcert=/app/certs/trust-bundle.pem` is the CA bundle, **as a path inside
the container**. The analyzer's Dockerfile builds it from Debian's public roots
plus AWS's 108 regional RDS roots, so it covers both a modern RDS certificate
and an older self-signed one. A bundle sitting on the host is invisible from
inside the container.

**Do not carry `channel_binding=require` over from a Neon URL.** It fails
outright on an RDS instance whose password encryption is still `md5`, with an
error that never mentions md5. `verify.sh` reports which your instance uses.

---

## Operations

```bash
cd ~/fraud-analyzer-engine/deploy
C="docker compose --env-file .env.prod -f docker-compose.prod.yml"

$C logs -f analyzer          # follow the engine
$C logs --tail 100 caddy     # TLS and proxy problems
$C ps                        # what is running, and its health
$C restart analyzer          # restart one service
$C down                      # stop everything (RDS data is untouched)

$C exec analyzer fae list-users
$C exec analyzer fae create-admin
$C exec analyzer fae reset-password    # for a locked-out account
$C exec analyzer alembic current       # what schema version is live
```

**Backups.** All durable state is in RDS. Turn on automated backups in the RDS
console and set the retention window; nothing on the instance needs backing up
except `.env.prod`, and of that only `FAE_FERNET_KEY` is unrecoverable.

**Log rotation** is configured in the compose file (10 MB × 5 per service).
Without it, container logs fill the root volume, and a full root volume takes
down Docker and ssh at about the same moment.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Sign-in posts, succeeds, returns to the login page with no error | You are on `http://`. The session cookie is `Secure`, so the browser refuses to store it. | Use `https://`. The `:80` block in the Caddyfile should be redirecting; `verify.sh` section 5 checks it. |
| Browser warns about the certificate | Expected. It is Caddy's own CA — no domain means no Let's Encrypt. | Accept it, or point a domain at the instance (see below). |
| `analyzer` never becomes healthy | It cannot reach RDS. | `verify.sh` section 2 names which of DNS, security group, TLS or credentials it is. |
| `connection timeout expired` in the analyzer log | RDS security group does not allow 5432 from this instance's security group. | Add that rule. Not an IP range. |
| An error about channel binding | `channel_binding=require` against an md5 instance. | Remove that parameter from `DATABASE_URL`. |
| `root certificate file ... does not exist` | `sslrootcert` points at a host path. | Use `/app/certs/trust-bundle.pem`. |
| Every saved connection fails to decrypt its password | `FAE_FERNET_KEY` changed. | Restore the old key. The startup log prints its fingerprint — compare across deploys. |
| Dashboard build is killed with no error | Out of memory. The Next build is the peak. | `bootstrap-ec2.sh` adds swap; re-run it, or build on a bigger instance. |
| Charts are slow and the responses are large | Compression is off. | `verify.sh` section 6. Check `encode zstd gzip` in the Caddyfile. |

---

## Getting a real certificate later

When a domain points at this instance, three edits and a restart:

1. `.env.prod`: `SWITCHBOARD_PUBLIC_HOST=switchboard.yourdomain.com`
2. `Caddyfile`: delete the `tls internal` line
3. `Caddyfile`: delete `auto_https disable_redirects` from the global block

```bash
./deploy.sh --no-build && ./verify.sh
```

Caddy provisions a Let's Encrypt certificate on the next start. Port 80 must be
open for the ACME challenge. `verify.sh` section 5 reports the issuer, so you
can confirm it is no longer Caddy's local CA.

---

## What this does not do

Named so it is a decision rather than an oversight.

- **One instance.** No load balancer, no autoscaling. `FAE_AUTO_MIGRATE=true`
  is correct for exactly this: two instances starting at once would race the
  same migration. Set it false and run migrations as a release step before
  adding a second.
- **No off-box log shipping.** Logs are JSON on the instance's disk, rotated.
  Adding CloudWatch is a logging driver change in the compose file.
- **No secret manager.** `.env.prod` is mode 0600 on the instance. Moving
  `FAE_FERNET_KEY` and the RDS password into AWS Secrets Manager is the next
  step if more than one person administers the box.
- **Rate-limit buckets are per worker process.** With `WEB_CONCURRENCY=2` the
  effective limit is twice what the setting says. It is a containment control,
  not accounting.
