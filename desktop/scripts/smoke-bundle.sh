#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
#
# Run the Python inside a built CodeNib.app the way the app does: start
# `codenib wiki --preset graph` on a small repository, wait for its address,
# fetch the Wiki, read the symbol graph that Map and Impact draw from, and
# save a note through the printed session key. The app's binaries carry the
# hardened runtime, so this also shows that runtime lets CodeNib work.
set -euo pipefail

APP="$(cd "${1:?usage: smoke-bundle.sh path/to/CodeNib.app}" && pwd)"
PY="$APP/Contents/Resources/python/bin/python3.12"
WORK="$(mktemp -d)"
LOG="$WORK/wiki.log"
cleanup() {
  # codenib wiki stops its own services on SIGINT, as Ctrl-C does. Give it
  # time, then stop its backend and frontend too: killing only the parent
  # left them serving on these ports, and the next run talked to them.
  if [[ -n "${PID:-}" ]]; then
    kill -INT "$PID" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
    pkill -KILL -P "$PID" 2>/dev/null || true
    kill -KILL "$PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT
# The Overview names the repository folder, so a distinctive name proves the
# page was built for this repository.
REPO="$WORK/bundle-smoke-repository"

mkdir -p "$REPO/pkg"
cat > "$REPO/pkg/store.py" <<'PY'
class Store:
    def put(self, key, value):
        self.items = {**getattr(self, "items", {}), key: value}
PY
cat > "$REPO/pkg/api.py" <<'PY'
from pkg.store import Store


def remember(key, value):
    store = Store()
    store.put(key, value)
    return store
PY
printf '# tiny\n\nA bundle smoke repository.\n' > "$REPO/README.md"
git -C "$REPO" init --quiet
git -C "$REPO" add .
git -C "$REPO" -c user.name=smoke -c user.email=smoke@example.invalid commit --quiet -m init

PORT=38471
API=38472
for port in "$PORT" "$API"; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "port $port is already in use; stop what serves it and run again" >&2
    exit 1
  fi
done
(
  cd "$REPO"
  exec env -i HOME="$WORK/home" PATH="$APP/Contents/Resources/python/bin:/usr/bin:/bin" \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
    "$PY" -m codenib wiki "$REPO" --preset graph --no-open --port "$PORT" --api-port "$API"
) > "$LOG" 2>&1 &
PID=$!

URL=""
for _ in $(seq 1 300); do
  URL="$(grep -o 'http://127.0.0.1:[0-9]*/[^ ]*session=[A-Za-z0-9_-]*' "$LOG" | head -1 || true)"
  [[ -n "$URL" ]] && break
  if ! kill -0 "$PID" 2>/dev/null; then cat "$LOG"; echo "codenib wiki exited early" >&2; exit 1; fi
  sleep 1
done
[[ -n "$URL" ]] || { cat "$LOG"; echo "no Wiki address after 300 s" >&2; exit 1; }
echo "Wiki ready: $URL"

SESSION="${URL##*session=}"
REPO_ID="$(curl --fail --silent "http://127.0.0.1:$PORT/api/repos" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')"
curl --fail --silent "http://127.0.0.1:$PORT/api/repos/$REPO_ID/wiki/overview" | grep -qi "bundle-smoke-repository"
curl --fail --silent -X POST "http://127.0.0.1:$PORT/api/repos/$REPO_ID/annotations" \
  -H "Content-Type: application/json" -H "X-CodeNib-Session: $SESSION" \
  -d '{"kind":"note","text":"bundle smoke note","on":{"page":"overview","quote":"tiny"}}' > /dev/null
grep -rq "bundle smoke note" "$REPO/.codenib/notes"
curl --fail --silent "http://127.0.0.1:$PORT/api/repos/$REPO_ID/codemap?symbol=remember&direction=callees&depth=1" |
  "$PY" -c '
import json, sys
graph = json.load(sys.stdin)
names = " ".join(str(node) for node in graph.get("nodes", []))
if not graph.get("available") or "put" not in names:
    sys.exit(f"the symbol graph does not show remember() calling Store.put: {graph}")
'
echo "bundled CodeNib served the Wiki, built the symbol graph and saved a note"
