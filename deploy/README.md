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
| `api-docs.sh` | Opens an ssh tunnel to the engine's `/docs`, which is deliberately not public. |
| `ship-images.sh` | Builds both images elsewhere and sends them over ssh, so a small instance never builds. |
| `docker-compose.prod.yml` | The three services. |
| `docker-compose.rehearsal.yml` | Overlay adding a local TLS Postgres, for `rehearse.sh`. |
| `Caddyfile` | TLS, compression, security headers. |
| `deployed.log` | Appended by `deploy.sh`: what commit was deployed, and when. |

---

## First deploy

**Pick your route first.** The two differ only in *where the images get built*,
and on a small instance that is the whole difference between working and not:

| | Build on the instance | Build on your laptop, ship |
|---|---|---|
| Instance needs | ~2.5 GB RAM, ~8 GB free disk | ~400 MB RAM, ~3 GB free disk |
| Suits | t3.small and up | t2/t3.micro |
| Steps | 1 → 6 below | 1 → 3, then [Build on your laptop, ship to the instance](#build-on-your-laptop-ship-to-the-instance), then 5 → 6 |

`preflight.sh` tells you which you are on before anything is built. If it fails
the memory or disk check, take the ship route — it is not a workaround, it is
the better shape: building is a five-minute peak, and sizing an instance for
its worst five minutes is how you pay for a t3.small to idle.

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

If `preflight.sh` failed on memory or disk, stop here and go to
[Build on your laptop, ship to the instance](#build-on-your-laptop-ship-to-the-instance).
Come back at step 5.

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

On a small instance, redeploy the other way instead — build on your laptop and
ship: [Redeploying afterwards](#redeploying-afterwards).

---

## Reading the API documentation

`/docs` is deliberately unreachable from the internet, and `verify.sh` asserts
it returns 404 from the public address. That is not a misconfiguration to work
around: `/docs` is not a reference page, it is an interactive form that composes
and executes SQL against whichever customer database a connection points at,
with a Try-it-out button beside every endpoint.

Reach it over ssh instead:

```bash
./api-docs.sh ubuntu@YOUR-IP -i ~/.ssh/your-key.pem
# then open http://localhost:8899/docs
```

It looks up the analyzer container's address on the instance (it changes
whenever the stack is recreated, so a pasted-in address silently forwards to
nothing), forwards a local port to it, and closes when you press Ctrl-C.
Nothing is published on the instance and no configuration changes.

If you only want the contract rather than a live instance,
`contracts/openapi.json` in this repository is the same specification, and CI
fails if it drifts from the code.

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

**Try this first — it is instant and often enough.** A failed build leaves
layers behind, and they are usually the largest thing on the volume:

```bash
docker system df                  # what Docker is holding
docker system prune -af           # reclaim all of it
df -h /
```

The ship route needs about **3 GB free**. If pruning gets you there, stop here;
you never have to touch AWS.

If it does not, the volume itself is too small. A fresh Ubuntu AMI gives you
8 GB, of which the OS takes most.

#### Step 1 — enlarge the volume in AWS

This is the step that is easy to skip, and skipping it makes step 2 fail in a
way that looks like step 2 is broken:

```
NOCHANGE: partition 1 is size 14452703. it cannot be grown
```

That message means the *disk* is still 8 GB. `growpart` expands a partition
into free space on the disk; it cannot make the disk bigger. Only AWS can.

30 GiB is the free-tier ceiling, so this costs nothing:

> EC2 console → **Elastic Block Store → Volumes** → select the volume attached
> to this instance → **Actions → Modify volume** → Size `30` → **Modify**.
>
> No reboot, no downtime. State goes `in-use - optimizing` and is usable
> immediately.

Or from the instance, if it has an IAM role with `ec2:DescribeVolumes` and
`ec2:ModifyVolume` (`aws` is not preinstalled on Ubuntu:
`sudo snap install aws-cli --classic`):

```bash
TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
IID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/instance-id)
AZ=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/placement/availability-zone)
REGION=${AZ%?}

VOL=$(aws ec2 describe-volumes --region "$REGION" \
  --filters "Name=attachment.instance-id,Values=$IID" \
  --query 'Volumes[0].VolumeId' --output text)
echo "root volume: $VOL"

aws ec2 modify-volume --region "$REGION" --volume-id "$VOL" --size 30
```

#### Step 2 — confirm AWS actually did it

**Do not skip this.** It is the difference between step 3 working and step 3
printing `NOCHANGE` at you:

```bash
lsblk /dev/nvme0n1
```

The **disk** line must now read `30G`. If it still says `8G`, step 1 has not
taken effect yet — wait ten seconds and look again. The partition under it will
still show the old size; that is what step 3 fixes.

```
nvme0n1      259:0    0   30G  0 disk     <-- must say 30G before continuing
└─nvme0n1p1  259:1    0  6.9G  0 part /   <-- still small, that is expected
```

#### Step 3 — grow the partition and the filesystem

Two commands, because they are two different things and neither implies the
other. Note the argument style: `growpart` takes the disk and the partition
number **as separate arguments**, `resize2fs` takes the partition device.

```bash
sudo growpart /dev/nvme0n1 1      # disk, then partition number
sudo resize2fs /dev/nvme0n1p1     # the partition itself
df -h /                           # confirm
```

On a t2 instance the device is `/dev/xvda` and `/dev/xvda1` instead. `lsblk`
tells you which you have.

### Not enough memory

**On the ship route you can skip this entirely.** Running the stack needs under
400 MB; the 2.5 GB peak is the *build*, and the build happens on your laptop.
Disk is the only constraint that matters on the instance.

If you are building on the instance, it needs swap. `bootstrap-ec2.sh` adds it
automatically under 3.5 GB of RAM, so check before creating one:

```bash
swapon --show
free -h
```

If that lists `/swapfile`, you already have it — leave it alone. Trying to
`fallocate` over an active swapfile fails with:

```
fallocate: fallocate failed: Text file busy
```

which means it is in use, not that anything is wrong. To replace it with a
bigger one, turn it off first — and note it needs the free disk to exist:

```bash
sudo swapoff /swapfile && sudo rm /swapfile
sudo fallocate -l 3G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
grep -q '^/swapfile ' /etc/fstab || \
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Even with swap, on 1 GB of RAM and 2 cores the dashboard build swaps hard and
can take 15–30 minutes. The section below avoids it entirely.

---

## Build on your laptop, ship to the instance

The instance never builds anything. You build both images where there are
resources, stream them over ssh, and start them there.

This is the recommended route on a t2/t3.micro, and it is worth reading even if
your instance is bigger: redeploys get much faster, and the instance stays
sized for what it serves rather than for its worst five minutes.

### Why

| | Building | Running |
|---|---|---|
| Memory | ~2.5 GB peak (the Next build) | under 400 MB |
| Disk | ~8 GB (node_modules, layer cache) | ~1 GB (two images) |
| Time | 3–5 min on a laptop, 15–30 min on a micro | — |

A micro instance runs this comfortably and cannot build it. So do not make it.

### What your laptop needs

Only three things, and no configuration file — `.env.prod` lives on the
instance, not here. Nothing you set up on this machine ends up inside an image.

**1. Docker, running.** Docker Desktop on macOS or Windows, Docker Engine on
Linux or WSL2. Check it:

```bash
docker run --rm hello-world
```

**2. Both repositories, side by side, on the branch you intend to deploy.**
`master` is production for both. Build from the same branch the instance runs,
or you will ship images that do not match the compose file and Caddyfile the
instance reads from its own checkout - `deploy.sh` records both, and
`deploy/deployed.log` is where that mismatch becomes visible after the fact.

The layout matters too: `ship-images.sh` looks for the dashboard at
`../../fraud-analyzer-dashboard` relative to `deploy/`.

```bash
git clone <engine-repo>    fraud-analyzer-engine
git clone <dashboard-repo> fraud-analyzer-dashboard

git -C fraud-analyzer-engine    checkout master
git -C fraud-analyzer-dashboard checkout master
```

Laid out differently? Point at it instead — either export it, or put
`DASHBOARD_CONTEXT=/path/to/dashboard` in `deploy/.env.prod` if you keep one
here:

```bash
DASHBOARD_CONTEXT=/somewhere/else/fraud-analyzer-dashboard ./ship-images.sh ...
```

**3. ssh to the instance, without a password prompt.** `ship-images.sh` runs
non-interactively, so the key has to be offered by the agent or named with
`-i`. Confirm before you start:

```bash
ssh -i ~/.ssh/your-key.pem ubuntu@YOUR-INSTANCE-IP true && echo ok
```

You also want roughly **10 GB free** on the laptop: npm's cache, the two
images, and Docker's build cache. `docker system df` shows what is already
there; `docker builder prune` reclaims the build cache alone.

**Optional but worth it the first time:** `./rehearse.sh` runs the entire stack
locally against a throwaway Postgres and tells you the images are good before
you spend the transfer on them. `./rehearse.sh --clean` when done.

### What the instance needs first

Once, before the first ship:

```bash
cd ~/fraud-analyzer-engine/deploy
./bootstrap-ec2.sh          # installs Docker; log out and back in afterwards
cp .env.prod.example .env.prod && chmod 600 .env.prod && nano .env.prod
```

The configuration lives there, not in the image — nothing environment-specific
is baked in, which is exactly why an image built on your laptop is correct on
the instance, and why no secret passes through the build or the transfer.

It also needs about **3 GB free** to load the images. If it does not have that,
do [Not enough disk](#not-enough-disk) first.

### Ship

On your **laptop**:

```bash
cd fraud-analyzer-engine/deploy
./ship-images.sh ubuntu@YOUR-INSTANCE-IP -i ~/.ssh/your-key.pem
```

Anything after the host is passed straight to `ssh`, so `-i`, `-p`, `-J` and
friends all work. If your key is already in the agent (`ssh-add`), drop the
`-i`.

What it does, and what it refuses to do:

1. Checks ssh works non-interactively and that Docker is usable as that user —
   before spending minutes on a build it cannot deliver.
2. Checks the instance has room to load the images.
3. Builds `switchboard-analyzer:latest` and `switchboard-dashboard:latest` with
   plain `docker build`. Not `docker compose build`, which interpolates the
   whole compose file first and would demand `DATABASE_URL` and
   `FAE_FERNET_KEY` on your laptop just to compile TypeScript.
4. Streams them: `docker save | gzip -1 | ssh 'gunzip | docker load'`. No
   temporary tarball at either end — which is the point when the instance's
   disk is the constraint. Measured: 525 MB of images, **200 MB on the wire**.
5. Compares image IDs afterwards. Presence is not enough: an older image of the
   same name would otherwise read as success and you would keep running last
   week's build.

Expect 3–6 minutes, most of it the transfer.

### Doing it by hand

The script is five commands with checks around them. If you want to run a step
yourself — debugging a build, shipping only one of the two images, or working
somewhere ssh piping is awkward — this is all it does:

```bash
cd fraud-analyzer-engine/deploy

# 1. build both images, tagged with the names the compose file expects
docker build -t switchboard-analyzer:latest  ../services/analyzer
docker build -t switchboard-dashboard:latest ../../fraud-analyzer-dashboard

# 2. what you are about to send (docker images takes only one name, so glob)
docker images 'switchboard-*'

# 3. stream both to the instance
docker save switchboard-analyzer:latest switchboard-dashboard:latest \
  | gzip -1 \
  | ssh -i ~/.ssh/your-key.pem ubuntu@YOUR-INSTANCE-IP 'gunzip | docker load'

# 4. confirm they landed, and are the same build
docker image inspect switchboard-analyzer:latest --format '{{.Id}}'
ssh -i ~/.ssh/your-key.pem ubuntu@YOUR-INSTANCE-IP \
  "docker image inspect switchboard-analyzer:latest --format '{{.Id}}'"
```

The tags in step 1 are not cosmetic. `docker-compose.prod.yml` names both
services' images explicitly, and `deploy.sh --no-build` starts whatever carries
those names — tag them anything else and compose will try to build instead.

Shipping just one image is the same command with one name:

```bash
docker save switchboard-dashboard:latest | gzip -1 \
  | ssh -i ~/.ssh/your-key.pem ubuntu@YOUR-INSTANCE-IP 'gunzip | docker load'
```

If ssh piping is blocked or unreliable, go via a file instead — this needs the
space on both ends, which is the tradeoff the streaming form avoids:

```bash
docker save switchboard-analyzer:latest switchboard-dashboard:latest | gzip -1 > images.tgz
scp -i ~/.ssh/your-key.pem images.tgz ubuntu@YOUR-INSTANCE-IP:/tmp/
ssh -i ~/.ssh/your-key.pem ubuntu@YOUR-INSTANCE-IP 'gunzip -c /tmp/images.tgz | docker load && rm /tmp/images.tgz'
rm images.tgz
```

### Start

On the **instance**:

```bash
cd ~/fraud-analyzer-engine/deploy
./deploy.sh --no-build
./verify.sh
```

`--no-build` is what makes the whole thing worthwhile: compose finds the images
by the names `ship-images.sh` tagged them with and starts them without building.
Migrations still run at analyzer startup, as usual.

Then, on a first deploy only:

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml \
    exec analyzer fae create-admin
```

### Redeploying afterwards

```bash
# laptop: after pulling or committing changes in either repository
cd fraud-analyzer-engine/deploy
./ship-images.sh ubuntu@YOUR-INSTANCE-IP -i ~/.ssh/your-key.pem

# instance
cd ~/fraud-analyzer-engine/deploy
git pull                      # picks up compose/Caddyfile/script changes
./deploy.sh --no-build && ./verify.sh
```

`git pull` on the instance still matters: the compose file, the Caddyfile and
the scripts are read from the checkout, not from the image.

Docker layer caching means the second and later ships are much faster — usually
only the layers that actually changed are rebuilt, though the transfer sends
whole layers, so a change to application source moves more bytes than a change
to a comment.

### If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `Cannot ssh to ... non-interactively` | key not offered | `ssh-add ~/.ssh/your-key.pem`, or pass `-i` |
| `docker is not usable as that user` | not in the docker group yet | run `./bootstrap-ec2.sh` on the instance, then log out and back in |
| `Only N GB free on the instance` | root volume too small | [Not enough disk](#not-enough-disk) |
| Dashboard build killed with no error | your laptop ran out of memory too | close things, or build on a bigger machine |
| Transfer stops partway | connection dropped | nothing on the instance changed — `docker load` applies an image only once the stream completes. Run it again. |
| `deploy.sh --no-build` says an image is missing | ship did not finish, or names drifted | `ssh HOST 'docker images \| grep switchboard'` — expect `switchboard-analyzer` and `switchboard-dashboard` |

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
