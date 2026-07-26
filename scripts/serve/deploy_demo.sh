#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
#
# Deploy the newest main to the CodeNib demo box (backend, frontend, landing).
#
# Run ON the serving box:   bash "$HOME/CodeNib/scripts/serve/deploy_demo.sh" [mode]
# CI runs it through a command-restricted SSH key; the mode arrives as the
# SSH command. Server-local settings come from ~/.codenib-demo.env.
#
# Modes:
#   normal (default)  fast-forward to origin/main, rebuild + restart what changed
#   force             rebuild + restart everything regardless of the diff
#   dry               fetch and report what a normal run would do; change nothing
set -euo pipefail

MODE="${1:-normal}"
case "$MODE" in
  normal|force|dry) ;;
  *) echo "usage: deploy_demo.sh [normal|force|dry]" >&2; exit 2 ;;
esac

source "$HOME/.codenib-demo.env"
cd "$CODENIB_REPO"

# Tracked modifications block a deploy (they would conflict with the pull);
# untracked files are fine — the box keeps a few local artifacts around.
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "deploy aborted: working tree has local changes:" >&2
  git status --short --untracked-files=no >&2
  echo "land them upstream or discard them, then rerun." >&2
  exit 1
fi

BEFORE=$(git rev-parse HEAD)
git fetch -q origin main
AFTER=$(git rev-parse origin/main)
CHANGED=$(git diff --name-only "$BEFORE" "$AFTER")

if [[ "$BEFORE" == "$AFTER" && "$MODE" == "normal" ]]; then
  echo "already up to date (main @ ${BEFORE:0:9})"
  exit 0
fi

need() {
  [[ "$MODE" == "force" ]] && return 0
  grep -qE "$1" <<<"$CHANGED"
}

echo "deploying ${BEFORE:0:9} -> ${AFTER:0:9} (mode: $MODE)"
if [[ "$MODE" == "dry" ]]; then
  need '^landing/' && echo "would sync: landing"
  need '^(docs/|mkdocs\.yml)' && echo "would rebuild: docs site"
  need '^pyproject\.toml' && echo "would reinstall: python package"
  need '^web/' && echo "would rebuild + restart: frontend"
  need '^(codenib/|qa_config\.yaml|scripts/serve/run_backend\.sh)' \
    && echo "would restart: backend"
  exit 0
fi

git merge -q --ff-only "$AFTER"

if need '^landing/'; then
  rsync -a --delete "$CODENIB_REPO/landing/" "$CODENIB_LANDING_DIR/"
  echo "landing synced to $CODENIB_LANDING_DIR"
fi

if need '^(docs/|mkdocs\.yml)'; then
  "$CODENIB_MKDOCS_VENV/bin/python" -m mkdocs build --strict \
    -d "$CODENIB_DOCS_SITE_DIR"
  echo "docs site rebuilt at $CODENIB_DOCS_SITE_DIR"
fi

if need '^pyproject\.toml'; then
  "$CODENIB_VENV/bin/pip" install -q -e .
  echo "python package reinstalled"
fi

FRONTEND=0
if need '^web/'; then
  FRONTEND=1
  export NVM_DIR="$HOME/.nvm"
  . "$NVM_DIR/nvm.sh"
  nvm use --lts >/dev/null
  cd web
  if need '^web/package(-lock)?\.json'; then
    npm ci --no-audit --no-fund
  fi
  npm run build
  cd ..
fi

# Kill whatever still listens on a port (covers sessions from older setups).
free_port() {
  local pid
  pid=$(ss -tlnp 2>/dev/null | awk -v p=":$1\$" '$4 ~ p' \
    | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
  if [[ -n "${pid:-}" ]]; then
    kill "$pid" 2>/dev/null || true
    sleep 1
  fi
}

restart() { # restart <tmux-session> <run-script> <port>
  tmux kill-session -t "$1" 2>/dev/null || true
  free_port "$3"
  tmux new-session -d -s "$1" "bash $CODENIB_REPO/scripts/serve/$2"
}

if need '^(codenib/|qa_config\.yaml|scripts/serve/run_backend\.sh)'; then
  restart nib-backend run_backend.sh 8000
  echo -n "waiting for backend (index load takes a while)"
  ok=0
  for _ in $(seq 1 120); do
    if curl -sf http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
      ok=1
      break
    fi
    echo -n "."
    sleep 2
  done
  echo
  if [[ $ok -ne 1 ]]; then
    echo "backend failed to become healthy; see ~/nib-backend.log" >&2
    exit 1
  fi
fi

if [[ $FRONTEND -eq 1 ]]; then
  restart nib-frontend run_frontend.sh 3000
  sleep 6
fi

fail=0
check() { # check <name> <url>
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$2" || echo 000)
  echo "$1: $code"
  [[ "$code" == "200" ]] || fail=1
}
check backend http://127.0.0.1:8000/api/health
check frontend http://127.0.0.1:3000/
check docs http://127.0.0.1:7880/
check demo https://demo.codenib.ai/
check landing https://codenib.ai/
exit $fail
