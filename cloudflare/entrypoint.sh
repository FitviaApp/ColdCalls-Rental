#!/usr/bin/env bash
set -euo pipefail

cd /app

echo "[entrypoint] restoring database from R2 (if configured)..."
python scripts/r2_db_sync.py restore || true

echo "[entrypoint] starting campaign worker..."
python worker.py &
WORKER_PID=$!

echo "[entrypoint] starting uvicorn on ${APP_HOST:-0.0.0.0}:${APP_PORT:-8000}..."
uvicorn app.main:app --host "${APP_HOST:-0.0.0.0}" --port "${APP_PORT:-8000}" &
APP_PID=$!

echo "[entrypoint] starting periodic R2 database backup..."
(
  while true; do
    sleep "${DB_BACKUP_INTERVAL_SECONDS:-60}"
    python scripts/r2_db_sync.py backup || true
  done
) &
BACKUP_PID=$!

shutdown() {
  echo "[entrypoint] shutting down..."
  kill -TERM "${WORKER_PID}" "${APP_PID}" "${BACKUP_PID}" 2>/dev/null || true
  wait "${WORKER_PID}" 2>/dev/null || true
  wait "${APP_PID}" 2>/dev/null || true
  wait "${BACKUP_PID}" 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

wait -n "${WORKER_PID}" "${APP_PID}" "${BACKUP_PID}"
echo "[entrypoint] a child process exited; shutting down remaining processes..."
shutdown
