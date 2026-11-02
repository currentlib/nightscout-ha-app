#!/bin/bash
# Builds a Nightscout release in staging, swaps it in, and rolls back if it
# doesn't come up. Runs as the unprivileged nightscout user. Any early exit
# leaves `current` untouched, so the running version keeps serving.
#
#   update.sh           check for a release and install it
#   update.sh recover   finish a rollback interrupted by a crash (run at boot)
set -uo pipefail

REPO=nightscout/cgm-remote-monitor
SEMVER=/usr/local/lib/node_modules/npm/node_modules/semver
STAGING=$NS_ROOT/staging
RELEASES=$NS_ROOT/releases
MARKER=$NS_STATE/updating
FAILED=$NS_STATE/failed_version
INSTALLED=$NS_STATE/version
PROBE_SECONDS=${NS_PROBE_SECONDS:-90}

log() { echo "[nightscout-update] $*"; }

# Prints a URL's body. Node's fetch, so the image needs no curl or apt packages.
get() {
  node -e '
    fetch(process.argv[1], {headers: {"User-Agent": "nightscout-ha-app"}, signal: AbortSignal.timeout(120000)})
      .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.arrayBuffer(); })
      .then((b) => process.stdout.write(Buffer.from(b)))
      .catch((e) => { console.error(e.message); process.exit(1); });
  ' "$1"
}

current_version() {
  [ -e "$NS_ROOT/current/package.json" ] || return 0
  jq -r .version "$NS_ROOT/current/package.json"
}

restart_nightscout() {
  kill "$(cat "$NS_ROOT/serve.pid")" 2>/dev/null
}

roll_back() {
  log "ERROR: version $1 did not come up, rolling back"
  echo "$1" > "$FAILED"
  if [ -L "$NS_ROOT/previous" ]; then
    mv -T "$NS_ROOT/previous" "$NS_ROOT/current"
  else
    rm -f "$NS_ROOT/current"
  fi
  rm -f "$MARKER"
  log "Now on version: $(current_version)"
}

if [ "${1:-}" = recover ]; then
  [ -f "$MARKER" ] && roll_back "$(cat "$MARKER")"
  exit 0
fi

rm -rf "$STAGING"
installed=$(current_version)

# A leading v is optional: upstream has used both forms for release tags.
if [ "$NS_WANTED" != latest ]; then
  tag=$NS_WANTED
elif [ -z "$installed" ] && [ -s "$INSTALLED" ]; then
  # Restored from a backup, which excludes the code: rebuild what was running.
  tag=$(cat "$INSTALLED")
  log "No build found, restoring version $tag"
elif [ -n "$installed" ] && [ "$NS_AUTO_UPDATE" != true ]; then
  exit 0
elif ! tag=$(get "https://api.github.com/repos/$REPO/releases/latest" | jq -re .tag_name); then
  log "WARNING: could not reach GitHub, keeping version ${installed:-none}"
  exit 0
fi

target=${tag#v}
[ "$target" = "$installed" ] && exit 0

if [ "$NS_WANTED" = latest ] && [ "$(cat "$FAILED" 2>/dev/null)" = "$target" ]; then
  log "WARNING: skipping $target, it failed to start before. Pin a version to retry it."
  exit 0
fi

mkdir -p "$RELEASES"

if [ ! -d "$RELEASES/$target" ]; then
  if ! engines=$(get "https://raw.githubusercontent.com/$REPO/$tag/package.json" | jq -re '.engines.node // "*"'); then
    log "WARNING: could not read package.json for $tag, keeping version ${installed:-none}"
    exit 0
  fi
  if ! node -e 'process.exit(require(process.argv[1]).satisfies(process.version, process.argv[2]) ? 0 : 1)' "$SEMVER" "$engines"; then
    log "WARNING: Nightscout $target needs Node $engines but this image has $(node -v)."
    log "WARNING: The app image needs a rebuild with a newer Node. Keeping version ${installed:-none}."
    exit 0
  fi

  log "Building Nightscout $target in the background, this can take 20 minutes"
  mkdir -p "$STAGING"
  if ! get "https://github.com/$REPO/archive/refs/tags/$tag.tar.gz" | tar -xz --strip-components=1 -C "$STAGING"; then
    log "ERROR: download of $tag failed, keeping version ${installed:-none}"
    rm -rf "$STAGING"
    exit 0
  fi

  # Dies before anything else if RAM runs out: mongod must never be the victim.
  # 384 MB is the smallest heap the v15.0.9 webpack build survives.
  if ! (
    cd "$STAGING" || exit 1
    echo 1000 2>/dev/null > /proc/self/oom_score_adj
    NODE_ENV=production NODE_OPTIONS=--max-old-space-size=512 \
      nice -n 19 npm ci --omit=optional --omit=dev --no-audit --no-fund --cache "$STAGING/.npm-cache"
  ); then
    log "ERROR: build of $target failed, keeping version ${installed:-none}"
    rm -rf "$STAGING"
    exit 0
  fi

  rm -rf "$STAGING/.npm-cache"
  mv -T "$STAGING" "$RELEASES/$target"
fi

log "Switching to Nightscout $target"
old=$(readlink "$NS_ROOT/current" 2>/dev/null)
echo "$target" > "$MARKER"
if [ -n "$old" ]; then
  ln -sfn "$old" "$NS_ROOT/previous.new"
  mv -T "$NS_ROOT/previous.new" "$NS_ROOT/previous"
else
  rm -f "$NS_ROOT/previous"
fi
ln -sfn "releases/$target" "$NS_ROOT/current.new"
mv -T "$NS_ROOT/current.new" "$NS_ROOT/current"
restart_nightscout

hash=$(printf %s "$API_SECRET" | sha1sum | cut -d' ' -f1)
probe() {
  node -e '
    fetch("http://127.0.0.1:1337/api/v1/status.json", {headers: {"api-secret": process.argv[1]}, signal: AbortSignal.timeout(5000)})
      .then((r) => r.json())
      .then((s) => process.exit(s.version === process.argv[2] ? 0 : 1))
      .catch(() => process.exit(1));
  ' "$hash" "$target" 2>/dev/null
}

for _ in $(seq 1 "$PROBE_SECONDS"); do
  if probe; then
    rm -f "$MARKER"
    echo "$target" > "$INSTALLED"
    previous=$(readlink "$NS_ROOT/previous" 2>/dev/null)
    for dir in "$RELEASES"/*; do
      name=releases/${dir##*/}
      [ "$name" = "releases/$target" ] || [ "$name" = "$previous" ] || rm -rf "$dir"
    done
    log "Nightscout $target is running"
    exit 0
  fi
  sleep 1
done

roll_back "$target"
restart_nightscout
