#!/bin/bash
# End-to-end test of install, update, rollback, restore and shutdown. Needs Docker,
# Node, and internet on the host. Fixtures are real Nightscout v15.0.9, relabelled.
#   tests/run.sh
set -uo pipefail
cd "$(dirname "$0")/.."

SOURCE=v15.0.9 C=nightscout-ha-test CACHE=tests/.cache
SECRET=not-a-real-secret-1234
HASH=$(printf %s "$SECRET" | sha1sum | cut -d' ' -f1)
fails=0

check() { # description, expected, actual
  if [ "$2" = "$3" ]; then echo "ok    $1"; else echo "FAIL  $1: expected '$2', got '$3'"; fails=$((fails + 1)); fi
}
ex() { docker exec "$C" "$@"; }
logs() { docker logs "$C" 2>&1; }
wait_log() { # text, seconds
  for _ in $(seq 1 "$2"); do logs | grep -qF "$1" && return 0; sleep 1; done
  echo "timed out waiting for: $1"; logs | tail -15; return 1
}
wait_ns() { # version, seconds
  for _ in $(seq 1 "$2"); do [ "$(ex version 2>/dev/null)" = "$1" ] && return 0; sleep 1; done
}
http() { ex http "$@"; }
restart() { docker stop -t 60 "$C" >/dev/null; docker start "$C" >/dev/null; }

fixtures() {
  [ -d "$CACHE/node_modules" ] && [ -f "$CACHE/$SOURCE.done" ] && return 0
  rm -rf "$CACHE"; mkdir -p "$CACHE/src" "$CACHE/bin"
  curl -sSL "https://github.com/nightscout/cgm-remote-monitor/archive/refs/tags/$SOURCE.tar.gz" | tar -xz --strip-components=1 -C "$CACHE/src"
  (cd "$CACHE/src" && NODE_ENV=production npm ci --omit=optional --omit=dev --no-audit --no-fund) || exit 1
  mv "$CACHE/src/node_modules" "$CACHE/node_modules"
  touch "$CACHE/$SOURCE.done"
}

release() { # version, engines
  rm -rf "$CACHE/tmp"; cp -a "$CACHE/src" "$CACHE/tmp"
  jq --arg v "$1" --arg e "$2" '.version = $v | .engines.node = $e' "$CACHE/src/package.json" > "$CACHE/tmp/package.json"
  cp "$CACHE/tmp/package.json" "$CACHE/pkg-$1.json"
  [ "${3:-}" = broken ] && echo 'process.exit(1)' > "$CACHE/tmp/lib/server/server.js"
  tar -czf "$CACHE/ns-$1.tgz" -C "$CACHE/tmp" --transform 's,^\.,nightscout,' .
  rm -rf "$CACHE/tmp"
}

fixtures
cat > "$CACHE/fake-github.js" <<'E'
// Loaded with --require. Serves GitHub from /test files, passes localhost through.
const fs = require('fs'), real = globalThis.fetch;
globalThis.fetch = async (url, opts) => {
  url = String(url);
  if (url.includes('127.0.0.1')) return real(url, opts);
  if (fs.existsSync('/test/offline')) throw new Error('offline');
  if (url.endsWith('/releases/latest')) return new Response(JSON.stringify({tag_name: 'v' + fs.readFileSync('/test/latest', 'utf8').trim()}));
  const v = url.match(/v?(\d+\.\d+\.\d+)/)[1];
  return new Response(fs.readFileSync(url.endsWith('package.json') ? `/test/pkg-${v}.json` : `/test/ns-${v}.tgz`));
};
E
cat > "$CACHE/bin/npm" <<'E'
#!/bin/bash
# Stands in for `npm ci`. The sleep keeps the build slow enough to observe.
sleep 3
[ -e /test/npm-fail ] && { echo 'npm ERR! simulated failure'; exit 1; }
cp -a /test/node_modules .
E
cat > "$CACHE/bin/version" <<E
#!/bin/bash
node -e 'fetch("http://127.0.0.1:1337/api/v1/status.json",{headers:{"api-secret":"$HASH"}}).then(r=>r.json()).then(s=>console.log(s.version)).catch(()=>process.exit(1))'
E
cat > "$CACHE/bin/http" <<'E'
#!/bin/bash
# http path [secret-hash] [post-body]: prints the status code.
node -e 'const [p,h,b]=process.argv.slice(1);fetch("http://127.0.0.1:1337"+p,{method:b?"POST":"GET",body:b||undefined,headers:{"api-secret":h||"","content-type":"application/json"}}).then(r=>console.log(r.status)).catch(()=>console.log(0))' "$@"
E
release 15.0.8 '>=20.x'
release 15.0.9 '>=20.x'
release 15.0.10 '>=20.x'
release 98.0.0 '>=99.x'
release 99.0.0 '>=20.x' broken
jq -n --arg s "$SECRET" '{api_secret: $s, nightscout_version: "latest", auto_update: true, display_units: "mmol",
  enable: "careportal", time_format: "24", language: "en",
  extra_env: ["NODE_OPTIONS=--require /test/fake-github.js", "NS_PROBE_SECONDS=20"]}' > "$CACHE/options.json"

docker build -q -t nightscout-ha-app:test nightscout >/dev/null || exit 1
docker build -q -t nightscout-ha-app:fixture -f tests/Dockerfile "$CACHE" >/dev/null || exit 1
docker rm -f "$C" >/dev/null 2>&1
docker create --init --name "$C" --stop-timeout 60 nightscout-ha-app:fixture >/dev/null
docker cp "$CACHE/options.json" "$C:/data/options.json"
docker start "$C" >/dev/null
trap 'docker rm -f "$C" >/dev/null 2>&1' EXIT

ex sh -c 'echo 15.0.8 > /test/latest'

echo '--- first install'
wait_log 'Building Nightscout 15.0.8' 90 || exit 1
check 'placeholder answers during the build' 200 "$(http /)"
wait_log 'Nightscout 15.0.8 is running' 60
check 'installs latest' 15.0.8 "$(ex version)"
check 'nightscout does not run as root' nightscout "$(ex sh -c 'ps -o user= -C node | sort -u | head -1')"
check 'mongod does not run as root' mongodb "$(ex sh -c 'ps -o user= -C mongod | sort -u | head -1')"

echo '--- auth'
check 'anonymous read denied' 401 "$(http /api/v1/entries.json)"
check 'watchdog URL answers without auth' 200 "$(http /)"
check 'anonymous write denied' 401 "$(http /api/v1/entries.json '' '[{"type":"sgv","sgv":100,"date":1790000000000}]')"
check 'upload with secret works' 200 "$(http /api/v1/entries.json "$HASH" '[{"type":"sgv","sgv":100,"date":1790000000000}]')"

echo '--- update while serving'
ex sh -c 'echo 15.0.9 > /test/latest'
restart
wait_log 'Building Nightscout 15.0.9' 60 || exit 1
wait_ns 15.0.8 30
check 'old version serves during the build' 15.0.8 "$(ex version)"
wait_log 'Nightscout 15.0.9 is running' 60
check 'updates to new release' 15.0.9 "$(ex version)"
check 'keeps previous for rollback' releases/15.0.8 "$(ex readlink /data/nightscout/previous)"
check 'data survives update' 1 "$(ex mongo --quiet nightscout --eval 'db.entries.count()')"

echo '--- failed build'
ex sh -c 'echo 15.0.10 > /test/latest; touch /test/npm-fail'
restart
wait_log 'build of 15.0.10 failed' 60 || exit 1
wait_ns 15.0.9 30
check 'failed build keeps the old version' 15.0.9 "$(ex version)"
check 'staging is cleaned up' no "$(ex sh -c 'test -e /data/nightscout/staging && echo yes || echo no')"
ex rm /test/npm-fail

echo '--- engines check'
ex sh -c 'echo 98.0.0 > /test/latest'
restart
wait_log 'needs Node >=99.x' 60 || exit 1
wait_ns 15.0.9 30
check 'incompatible Node keeps the old version' 15.0.9 "$(ex version)"

echo '--- release that builds but will not start'
ex sh -c 'echo 99.0.0 > /test/latest'
restart
wait_log 'version 99.0.0 did not come up' 90 || exit 1
wait_ns 15.0.9 30
check 'rolls back automatically' 15.0.9 "$(ex version)"
check 'remembers the bad version' 99.0.0 "$(ex cat /data/state/failed_version)"
restart
wait_log 'skipping 99.0.0' 60 || exit 1
check 'does not rebuild the bad version' 1 "$(logs | grep -c 'Building Nightscout 99.0.0')"

echo '--- crash during the probe'
ex sh -c 'cd /data/nightscout && ln -sfn releases/99.0.0 current.new && ln -sfn releases/15.0.9 previous && mv -T current.new current && echo 99.0.0 > /data/state/updating'
restart
wait_ns 15.0.9 60
check 'interrupted update rolls back at boot' 15.0.9 "$(ex version)"
check 'marker is cleared' no "$(ex sh -c 'test -e /data/state/updating && echo yes || echo no')"

echo '--- GitHub unreachable'
ex touch /test/offline
restart
wait_log 'could not reach GitHub' 60 || exit 1
wait_ns 15.0.9 30
check 'offline starts the installed version' 15.0.9 "$(ex version)"
ex rm /test/offline

echo '--- pinned version and auto_update off'
docker stop -t 60 "$C" >/dev/null
jq '.nightscout_version = "15.0.8" | .auto_update = false' "$CACHE/options.json" > "$CACHE/options2.json"
docker cp "$CACHE/options2.json" "$C:/data/options.json"
docker start "$C" >/dev/null
wait_ns 15.0.8 90
check 'pin moves back without a rebuild' 15.0.8 "$(ex version)"
docker stop -t 60 "$C" >/dev/null
jq '.auto_update = false' "$CACHE/options.json" > "$CACHE/options2.json"
docker cp "$CACHE/options2.json" "$C:/data/options.json"
docker start "$C" >/dev/null
sleep 15
check 'auto_update false stays put' 15.0.8 "$(ex version)"
docker stop -t 60 "$C" >/dev/null
docker cp "$CACHE/options.json" "$C:/data/options.json"
docker start "$C" >/dev/null
sleep 3

echo '--- restore: code is not in the backup'
ex sh -c 'echo 15.0.9 > /test/latest'
wait_ns 15.0.8 60
docker exec "$C" sh -c 'echo 15.0.8 > /data/state/version; rm -rf /data/nightscout'
docker stop -t 60 "$C" >/dev/null
docker start "$C" >/dev/null
wait_log 'restoring version 15.0.8' 60 || exit 1
wait_ns 15.0.8 60
check 'restore rebuilds the recorded version' 15.0.8 "$(ex version)"
check 'database survives restore' 1 "$(ex mongo --quiet nightscout --eval 'db.entries.count()')"

echo '--- shutdown'
start=$(date +%s)
docker stop -t 60 "$C" >/dev/null
check 'stops cleanly before the timeout' yes "$([ $(($(date +%s) - start)) -lt 30 ] && echo yes || echo no)"
check 'container exit code' 0 "$(docker inspect -f '{{.State.ExitCode}}' "$C")"
check 'mongod exit code' 1 "$(logs | tail -3 | grep -c 'mongod stopped with exit code 0')"
docker start "$C" >/dev/null
wait_ns 15.0.8 90
ex pkill -9 mongod
sleep 3
check 'mongod crash stops the container' false "$(docker inspect -f '{{.State.Running}}' "$C")"

echo
[ "$fails" -eq 0 ] && echo 'ALL PASSED' || { echo "$fails FAILED"; exit 1; }
