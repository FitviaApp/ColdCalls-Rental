#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/bin/common.sh
source "${SCRIPT_DIR}/common.sh"

PROJECT_DIR="$(resolve_project_dir)"
resolve_runtime_binaries "${PROJECT_DIR}"

cd "${PROJECT_DIR}"
CMD=("${PYTHON_BIN}" worker.py)

if [ "${COLDCALLS_STARTUP_DRY_RUN:-0}" = "1" ]; then
    print_command "${CMD[@]}"
    exit 0
fi

exec "${CMD[@]}"
