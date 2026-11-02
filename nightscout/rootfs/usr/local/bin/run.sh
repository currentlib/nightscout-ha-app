#!/bin/bash
# Runs mongod and Nightscout, and stops them in order. Updates happen in the
# background so a slow build never takes the running site down.
set -uo pipefail

export NS_ROOT=/data/nightscout NS_STATE=/data/state
OPTIONS=/data/options.json
mongod_pid='' serve_pid='' update_pid=''

log() { echo "[nightscout] $*"; }
opt() { jq -r --arg k "$1" '.[$k] // empty' "$OPTIONS"; }

shutdown() {
  trap - TERM INT
  log "Stopping"
  [ -n "$update_pid" ] && kill "$update_pid" 2>/dev/null
  if [ -n "$serve_pid" ]; then
    kill "$serve_pid" 2>/dev/null
    wait "$serve_pid" 2>/dev/null
  fi
  if [ -n "$mongod_pid" ]; then
    kill -TERM "$mongod_pid" 2>/dev/null
    wait "$mongod_pid"
    log "mongod stopped with exit code $?"
  fi
  exit 0
}
trap shutdown TERM INT

# Serves whichever release `current` points at, or the placeholder if there is
# none. update.sh swaps versions by repointing `current` and killing the child.
serve() {
  local child=''
  trap '[ -n "$child" ] && kill "$child" 2>/dev/null; exit 0' TERM
  while true; do
    if [ -e "$NS_ROOT/current/lib/server/server.js" ]; then
      (cd "$NS_ROOT/current" && exec gosu nightscout node lib/server/server.js) &
    else
      gosu nightscout node /usr/local/bin/placeholder.js &
    fi
    child=$!
    echo "$child" > "$NS_ROOT/serve.pid"
    wait "$child"
    sleep 1
  done
}

mkdir -p /data/mongo "$NS_ROOT" "$NS_STATE"
chown -R mongodb:mongodb /data/mongo
chown -R nightscout "$NS_STATE"
chown nightscout "$NS_ROOT"

# Everything the Nightscout process sees. Order matters: AUTH_DEFAULT_ROLES is a
# default extra_env can change, the block after extra_env cannot be overridden.
export AUTH_DEFAULT_ROLES=denied
export API_SECRET DISPLAY_UNITS ENABLE TIME_FORMAT LANGUAGE
API_SECRET=$(opt api_secret)
DISPLAY_UNITS=$(opt display_units)
ENABLE=$(opt enable)
TIME_FORMAT=$(opt time_format)
LANGUAGE=$(opt language)
base_url=$(opt base_url)
[ -n "$base_url" ] && export BASE_URL="$base_url"

while IFS= read -r pair; do
  [ -n "$pair" ] && export "$pair"
done < <(jq -r '.extra_env[]?' "$OPTIONS")

export MONGODB_URI=mongodb://127.0.0.1:27017/nightscout
export PORT=1337
export NODE_ENV=production
# Plain HTTP on this port. Without this Nightscout answers everything with a
# redirect to https, which breaks the watchdog and Home Assistant. TLS for
# outside access comes from the tunnel.
export INSECURE_USE_HTTP=true

export NS_WANTED NS_AUTO_UPDATE
NS_WANTED=$(opt nightscout_version)
NS_AUTO_UPDATE=$(jq -r '.auto_update' "$OPTIONS")

log "Starting MongoDB"
gosu mongodb mongod --dbpath /data/mongo --bind_ip 127.0.0.1 --quiet \
  --wiredTigerCacheSizeGB 0.25 \
  --setParameter diagnosticDataCollectionEnabled=false &
mongod_pid=$!

until mongo --quiet --eval 'db.runCommand({ping: 1}).ok' 2>/dev/null | grep -q '^1$'; do
  if ! kill -0 "$mongod_pid" 2>/dev/null; then
    log "ERROR: mongod exited during startup, see the log above"
    exit 1
  fi
  sleep 1
done
log "MongoDB is ready"

gosu nightscout update.sh recover
serve &
serve_pid=$!

gosu nightscout update.sh &
update_pid=$!

wait "$mongod_pid"
log "ERROR: mongod stopped unexpectedly"
mongod_pid=''
kill "$serve_pid" 2>/dev/null
exit 1
