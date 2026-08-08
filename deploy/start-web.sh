#!/bin/sh
set -e

# Wait briefly for the live machine's init to create the DB on the shared volume
# (only matters for the first deploy; idempotent after that).
for i in 1 2 3 4 5; do
  [ -f /data/polysim.db ] && break
  echo "waiting for /data/polysim.db (attempt $i/5)"
  sleep 2
done

exec python -m polysim.cli web \
  --host 0.0.0.0 \
  --port 8080 \
  --db /data/polysim.db
