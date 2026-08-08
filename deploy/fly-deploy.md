# Deploying PolySim to Fly.io — end-to-end from a terminal

One-time setup. After this, every code change just runs `fly deploy`.

## 0. Install flyctl

**Windows (PowerShell, one-time, run as user):**
```powershell
iwr https://fly.io/install.ps1 -useb | iex
```

After the installer finishes, restart your terminal so `flyctl` is on PATH.

**Mac / Linux:**
```bash
curl -L https://fly.io/install.sh | sh
```

## 1. Authenticate (the one browser step)

```bash
fly auth signup     # if you don't have an account yet
# or
fly auth login      # if you do — opens a browser tab once
```

You'll need to add a payment method. PolySim costs about **$3–7/month** total
(one 1GB shared-CPU machine + one 512MB machine + ~5GB volume).

## 2. Create the app + volume

From the repo root:

```bash
# Creates a Fly app tied to this directory (do NOT deploy yet).
# Pick your own globally-unique app name.
fly launch --no-deploy --copy-config --name my-polysim --region iad

# Create the persistent volume that holds polysim.db.
# Size it comfortably above your current DB (it grows to a few GB over months).
fly volumes create polysim_data --region iad --size 10 --yes
```

## 3. Push your secrets

```bash
fly secrets set \
  ALCHEMY_API_KEY=your_alchemy_key \
  ANTHROPIC_API_KEY=your_anthropic_key \
  TELEGRAM_BOT_TOKEN=your_bot_token \
  TELEGRAM_CHAT_ID=your_chat_id
```

These get injected as environment variables at runtime; they're encrypted at
rest and not visible in deploy logs.

## 4. Upload your existing DB (so you don't restart from zero)

This is the only place data could be lost — and only if you skip this step.
Copy the DB onto the volume **before** the first deploy:

```bash
# Spin up a one-off machine that mounts the volume, then scp the file onto it.
fly machine run --volume polysim_data:/data \
                --image alpine \
                --command "sleep 600" \
                --rm=false

# In another terminal, find the machine id:
fly machine list

# Then push the DB onto it:
fly sftp shell -a my-polysim
> put polysim.db /data/polysim.db
> exit

# Stop the helper machine:
fly machine destroy <machine-id> --force
```

(If you'd rather start fresh and have PolySim rebuild the cohort from scratch,
skip this step — `polysim init` runs on first deploy.)

## 5. Deploy

```bash
fly deploy
```

This builds the Docker image, pushes it to Fly's registry, and rolls out
both machines. Expect ~5–10 minutes the first time, ~2 min for subsequent
deploys.

## 6. Verify

```bash
# Status of both machines:
fly status

# Live tail of the orchestrator logs:
fly logs -i live

# Live tail of the web service:
fly logs -i web

# The web dashboard URL:
fly info | grep Hostname
# e.g. https://my-polysim.fly.dev/
```

## Day-to-day

```bash
fly deploy                       # ship a code change
fly logs                         # tail logs
fly ssh console                  # shell into the live machine
fly secrets set FOO=bar          # rotate a secret
fly machine restart -i live      # restart the orchestrator
fly volumes list                 # check volume size + region
```

## If it crashes

Fly's `restart_policy = "on-failure"` will retry up to 10 times with backoff.
You'll see attempts in `fly logs`. If a deploy genuinely breaks and you want
to roll back:

```bash
fly releases                     # find the previous good release ID
fly deploy --image <release-id>  # roll back
```

## Cost

| Resource | Spec | Monthly |
|---|---|---|
| `live` machine | shared-cpu-1x, 1 GB | ~$1.50 |
| `web` machine | shared-cpu-1x, 512 MB | ~$1.00 |
| Volume | 10 GB | $1.50 |
| Egress | first 100 GB free | $0 |
| **Total** | | **~$4–5** |

Polymarket WS traffic and Alchemy RPC stay well under 100 GB/month, so egress
is free.

## Why this won't lose data

- Volumes are durable storage that survive deploys, restarts, and machine
  crashes. They aren't replicated across regions, so the only failure mode
  is a Fly region-wide outage (rare; if you care, snapshot the volume nightly
  with `fly volumes snapshot create`).
- Container filesystem changes are discarded on every deploy — that's by
  design. The DB sits on the mounted `/data` volume, so it's safe.
- `polysim init` runs on every container start. It's idempotent (only
  applies migrations that haven't been applied yet) — so a deploy with new
  migrations upgrades the schema, but never wipes data.
- WAL mode is the SQLite default in our code; restarts mid-write are
  recoverable via the WAL replay on next open.

## Why it stays running

- No laptop dependency — the machine runs in Fly's datacenter on their power.
- `restart_policy = "on-failure"` retries on any non-zero exit.
- `min_machines_running = 1` means Fly won't sleep the container even if
  there's no inbound traffic.
- The watchdog inside PolySim sends Telegram alerts if ingest stalls for
  >5 min, so you know within minutes of a real problem rather than 12 days
  later.
