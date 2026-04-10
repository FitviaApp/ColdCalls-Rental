#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/bin/common.sh
source "${SCRIPT_DIR}/common.sh"

PROJECT_DIR="$(resolve_project_dir)"
resolve_runtime_binaries "${PROJECT_DIR}"

cd "${PROJECT_DIR}"

if [ "${UVICORN_USE_MODULE}" -eq 1 ]; then
    CMD=(
        "${UVICORN_LAUNCHER}"
        -m
        uvicorn
        app.main:app
        --host
        "${APP_HOST:-127.0.0.1}"
        --port
        "${APP_PORT:-8000}"
    )
else
    CMD=(
        "${UVICORN_LAUNCHER}"
        app.main:app
        --host
        "${APP_HOST:-127.0.0.1}"
        --port
        "${APP_PORT:-8000}"
    )
fi

if [ "${COLDCALLS_STARTUP_DRY_RUN:-0}" = "1" ]; then
    print_command "${CMD[@]}"
    exit 0
fi

exec "${CMD[@]}"
