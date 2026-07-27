#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_DIR="$ROOT/web"
STATE_DIR="${CODENIB_WEB_STATE_DIR:-$ROOT/.codenib_demo/server}"

BACKEND_HOST="${CODENIB_DEMO_HOST:-127.0.0.1}"
BACKEND_PORT="${CODENIB_DEMO_PORT:-8000}"
FRONTEND_HOST="${CODENIB_WEB_FRONTEND_HOST:-0.0.0.0}"
FRONTEND_PORT="${CODENIB_WEB_FRONTEND_PORT:-3000}"
PUBLIC_FRONTEND_HOST="${CODENIB_WEB_PUBLIC_FRONTEND_HOST:-localhost}"
# Backend URL used by the Vite development proxy. Browser code should use the
# same-origin /api path so remote/forwarded browsers don't try to reach their
# own 127.0.0.1:8000.
API_BASE="${CODENIB_API_BASE:-http://${BACKEND_HOST}:${BACKEND_PORT}}"

PYTHON_BIN="${CODENIB_WEB_PYTHON:-python}"
NODE_BIN="${CODENIB_WEB_NODE:-node}"
RECLAIM_PORTS="${CODENIB_WEB_RECLAIM_PORTS:-0}"

BACKEND_PID="$STATE_DIR/backend.pid"
FRONTEND_PID="$STATE_DIR/frontend.pid"
BACKEND_LOG="$STATE_DIR/backend.log"
FRONTEND_LOG="$STATE_DIR/frontend.log"

usage() {
  cat <<EOF
Usage: $0 <command>

Commands:
  start      Start the FastAPI backend and Vite frontend.
  stop       Stop managed backend/frontend processes.
  restart    Stop managed processes, then start both services.
  reclaim    Stop current-user listeners on the managed ports.
  status     Show PID, cwd, port, health, and log locations.
  logs       Print recent backend and frontend logs.
  follow     Follow backend and frontend logs.

Environment:
  CODENIB_WEB_PYTHON         Python executable for uvicorn (default: python)
  CODENIB_WEB_NODE           node executable for Vite (default: node)
  CODENIB_DEMO_HOST          Backend host (default: 127.0.0.1)
  CODENIB_DEMO_PORT          Backend port (default: 8000)
  CODENIB_WEB_FRONTEND_HOST  Vite bind host (default: 0.0.0.0)
  CODENIB_WEB_FRONTEND_PORT  Vite port (default: 3000)
  CODENIB_API_BASE           Backend URL for Vite /api proxy (default: http://127.0.0.1:8000)
  CODENIB_WEB_RECLAIM_PORTS  If 1, start/restart may stop current-user port owners.
EOF
}

ensure_state_dir() {
  mkdir -p "$STATE_DIR"
}

is_alive() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

pid_from_file() {
  local file="$1"
  [[ -f "$file" ]] && sed -n '1p' "$file" || true
}

pid_user() {
  ps -o user= -p "$1" 2>/dev/null | awk '{print $1}'
}

pid_cwd() {
  readlink "/proc/$1/cwd" 2>/dev/null || printf '?'
}

pid_cmd() {
  ps -o args= -p "$1" 2>/dev/null | sed 's/^ *//'
}

port_pids() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp "( sport = :$port )" 2>/dev/null \
      | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' \
      | sort -u
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u
  fi
}

print_pid_details() {
  local pid="$1"
  printf '    pid=%s user=%s cwd=%s\n' "$pid" "$(pid_user "$pid")" "$(pid_cwd "$pid")"
  printf '    cmd=%s\n' "$(pid_cmd "$pid")"
}

terminate_pid_group() {
  local pid="$1"
  local pgid
  if ! is_alive "$pid"; then
    return 0
  fi

  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | awk '{print $1}')"
  if [[ -n "$pgid" ]]; then
    kill -TERM "-$pgid" 2>/dev/null || true
  fi
  kill -TERM "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    if ! is_alive "$pid"; then
      return 0
    fi
    sleep 1
  done
  if [[ -n "$pgid" ]]; then
    kill -KILL "-$pgid" 2>/dev/null || true
  fi
  kill -KILL "$pid" 2>/dev/null || true
}

wait_for_pid_file() {
  local pid_file="$1"
  local label="$2"
  local pid

  for _ in 1 2 3 4 5 6 7 8 9 10; do
    pid="$(pid_from_file "$pid_file")"
    if is_alive "$pid"; then
      printf '%s' "$pid"
      return 0
    fi
    sleep 0.2
  done

  printf '%s pid file was not written or process exited early: %s\n' "$label" "$pid_file" >&2
  return 1
}

stop_service() {
  local name="$1"
  local pid_file="$2"
  local pid

  pid="$(pid_from_file "$pid_file")"
  if is_alive "$pid"; then
    printf 'Stopping %s pid=%s\n' "$name" "$pid"
    terminate_pid_group "$pid"
  elif [[ -n "$pid" ]]; then
    printf '%s pid file was stale: %s\n' "$name" "$pid"
  fi
  rm -f "$pid_file"
}

stop_all() {
  stop_service frontend "$FRONTEND_PID"
  stop_service backend "$BACKEND_PID"
}

reclaim_port() {
  local port="$1"
  local label="$2"
  local current_user
  local pids

  current_user="$(id -un)"
  mapfile -t pids < <(port_pids "$port")
  if ((${#pids[@]} == 0)); then
    return 0
  fi

  for pid in "${pids[@]}"; do
    if [[ "$(pid_user "$pid")" != "$current_user" ]]; then
      printf 'Refusing to stop %s port %s owned by another user:\n' "$label" "$port" >&2
      print_pid_details "$pid" >&2
      continue
    fi
    printf 'Reclaiming %s port %s from pid=%s\n' "$label" "$port" "$pid"
    print_pid_details "$pid"
    terminate_pid_group "$pid"
  done
}

reclaim_ports() {
  reclaim_port "$FRONTEND_PORT" frontend
  reclaim_port "$BACKEND_PORT" backend
}

require_port_available() {
  local port="$1"
  local label="$2"
  local pids

  mapfile -t pids < <(port_pids "$port")
  if ((${#pids[@]} == 0)); then
    return 0
  fi

  if [[ "$RECLAIM_PORTS" == "1" ]]; then
    reclaim_port "$port" "$label"
    mapfile -t pids < <(port_pids "$port")
    if ((${#pids[@]} == 0)); then
      return 0
    fi
  fi

  printf '%s port %s is already in use. Run status to inspect it, or set CODENIB_WEB_RECLAIM_PORTS=1.\n' "$label" "$port" >&2
  for pid in "${pids[@]}"; do
    print_pid_details "$pid" >&2
  done
  return 1
}

wait_for_http() {
  local label="$1"
  local url="$2"
  local attempts="${3:-60}"

  if ! command -v curl >/dev/null 2>&1; then
    return 0
  fi

  for _ in $(seq 1 "$attempts"); do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      printf '%s is ready: %s\n' "$label" "$url"
      return 0
    fi
    sleep 1
  done

  printf '%s did not become ready: %s\n' "$label" "$url" >&2
  return 1
}

start_backend() {
  local pid
  pid="$(pid_from_file "$BACKEND_PID")"
  if is_alive "$pid"; then
    printf 'Backend already running pid=%s\n' "$pid"
    return 0
  fi

  command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    printf 'Python executable not found: %s\n' "$PYTHON_BIN" >&2
    return 1
  }
  require_port_available "$BACKEND_PORT" backend

  : > "$BACKEND_LOG"
  BACKEND_PID_FILE="$BACKEND_PID" \
  ROOT="$ROOT" \
  PYTHON_BIN="$PYTHON_BIN" \
  BACKEND_HOST="$BACKEND_HOST" \
  BACKEND_PORT="$BACKEND_PORT" \
  setsid -f bash -c '
    echo $$ > "$BACKEND_PID_FILE"
    cd "$ROOT"
    exec "$PYTHON_BIN" -m uvicorn codenib.web.app:app --host "$BACKEND_HOST" --port "$BACKEND_PORT"
  ' >>"$BACKEND_LOG" 2>&1 </dev/null

  printf 'Started backend pid=%s log=%s\n' "$(wait_for_pid_file "$BACKEND_PID" backend)" "$BACKEND_LOG"
  wait_for_http backend "http://${BACKEND_HOST}:${BACKEND_PORT}/api/health" 60 || {
    tail -n 80 "$BACKEND_LOG" >&2 || true
    return 1
  }
}

start_frontend() {
  local pid
  pid="$(pid_from_file "$FRONTEND_PID")"
  if is_alive "$pid"; then
    printf 'Frontend already running pid=%s\n' "$pid"
    return 0
  fi

  command -v "$NODE_BIN" >/dev/null 2>&1 || {
    printf 'node executable not found: %s\n' "$NODE_BIN" >&2
    return 1
  }
  [[ -f "$WEB_DIR/node_modules/vite/bin/vite.js" ]] || {
    printf 'Missing Vite CLI. Run: make web-deps\n' >&2
    return 1
  }
  [[ -d "$WEB_DIR/node_modules" ]] || {
    printf 'Missing %s. Run: make web-deps\n' "$WEB_DIR/node_modules" >&2
    return 1
  }
  require_port_available "$FRONTEND_PORT" frontend

  : > "$FRONTEND_LOG"
  FRONTEND_PID_FILE="$FRONTEND_PID" \
  WEB_DIR="$WEB_DIR" \
  NODE_BIN="$NODE_BIN" \
  FRONTEND_HOST="$FRONTEND_HOST" \
  FRONTEND_PORT="$FRONTEND_PORT" \
  CODENIB_API_BASE="$API_BASE" \
  setsid -f bash -c '
    echo $$ > "$FRONTEND_PID_FILE"
    cd "$WEB_DIR"
    export CODENIB_API_BASE
    exec "$NODE_BIN" "$WEB_DIR/node_modules/vite/bin/vite.js" --host "$FRONTEND_HOST" --port "$FRONTEND_PORT"
  ' >>"$FRONTEND_LOG" 2>&1 </dev/null

  printf 'Started frontend pid=%s log=%s\n' "$(wait_for_pid_file "$FRONTEND_PID" frontend)" "$FRONTEND_LOG"
  wait_for_http frontend "http://${PUBLIC_FRONTEND_HOST}:${FRONTEND_PORT}/" 60 || {
    tail -n 80 "$FRONTEND_LOG" >&2 || true
    return 1
  }
}

start_all() {
  ensure_state_dir
  start_backend
  start_frontend
  printf '\nOpen http://%s:%s\n' "$PUBLIC_FRONTEND_HOST" "$FRONTEND_PORT"
}

show_service() {
  local label="$1"
  local pid_file="$2"
  local port="$3"
  local log_file="$4"
  local pid
  local port_owners

  pid="$(pid_from_file "$pid_file")"
  if is_alive "$pid"; then
    printf '%s: managed, running\n' "$label"
    print_pid_details "$pid"
  elif [[ -n "$pid" ]]; then
    printf '%s: managed pid stale (%s)\n' "$label" "$pid"
  else
    printf '%s: no managed pid\n' "$label"
  fi
  printf '    port=%s log=%s\n' "$port" "$log_file"

  mapfile -t port_owners < <(port_pids "$port")
  if ((${#port_owners[@]} > 0)); then
    printf '    listeners:\n'
    for owner in "${port_owners[@]}"; do
      print_pid_details "$owner"
    done
  else
    printf '    listeners: none\n'
  fi
}

status_all() {
  show_service backend "$BACKEND_PID" "$BACKEND_PORT" "$BACKEND_LOG"
  show_service frontend "$FRONTEND_PID" "$FRONTEND_PORT" "$FRONTEND_LOG"
  if command -v curl >/dev/null 2>&1; then
    printf 'backend health: '
    curl -fsS --max-time 3 "http://${BACKEND_HOST}:${BACKEND_PORT}/api/health" 2>/dev/null || printf 'unavailable'
    printf '\n'
    printf 'frontend: '
    if curl -fsS --max-time 3 "http://${PUBLIC_FRONTEND_HOST}:${FRONTEND_PORT}/" >/dev/null 2>&1; then
      printf 'ok\n'
    else
      printf 'unavailable\n'
    fi
  fi
}

logs_all() {
  printf '== backend: %s ==\n' "$BACKEND_LOG"
  tail -n 80 "$BACKEND_LOG" 2>/dev/null || true
  printf '\n== frontend: %s ==\n' "$FRONTEND_LOG"
  tail -n 80 "$FRONTEND_LOG" 2>/dev/null || true
}

follow_logs() {
  touch "$BACKEND_LOG" "$FRONTEND_LOG"
  tail -n 80 -f "$BACKEND_LOG" "$FRONTEND_LOG"
}

cmd="${1:-status}"
case "$cmd" in
  start)
    start_all
    ;;
  stop)
    ensure_state_dir
    stop_all
    ;;
  restart)
    ensure_state_dir
    stop_all
    [[ "$RECLAIM_PORTS" == "1" ]] && reclaim_ports
    start_all
    ;;
  reclaim)
    reclaim_ports
    ;;
  status)
    ensure_state_dir
    status_all
    ;;
  logs)
    logs_all
    ;;
  follow)
    ensure_state_dir
    follow_logs
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
