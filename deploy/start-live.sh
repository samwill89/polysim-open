#!/bin/sh
set -e

# Idempotent migration application against the mounted volume.
python -m polysim.cli init --db /data/polysim.db

# Web dashboard runs alongside the orchestrator on the SAME machine —
# Fly volumes only attach to one machine at a time, so we can't have a
# separate web container. Backgrounded; if it crashes the watchdog above
# isn't affected.
python -m polysim.cli web \
  --host 0.0.0.0 --port 8080 \
  --db /data/polysim.db &

# Replace this shell with the long-running orchestrator so signals
# (SIGINT on container stop) reach Python directly.
exec python -m polysim.cli live \
  --profiles systematic \
  --equity-variants smh_bh,momentum_slow \
  --evidence \
  --tag experiment_002 \
  --db /data/polysim.db
