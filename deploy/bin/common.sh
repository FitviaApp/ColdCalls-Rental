#!/usr/bin/env bash
set -euo pipefail

resolve_project_dir() {
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -n "${COLDCALLS_PROJECT_DIR:-}" ]; then
        cd "${COLDCALLS_PROJECT_DIR}"
        pwd
        return
    fi
    cd "${script_dir}/../.."
    pwd
}

resolve_virtualenv_bin_dir() {
    local project_dir="$1"
    if [ -x "${project_dir}/.venv/bin/python" ]; then
        printf '%s\n' "${project_dir}/.venv/bin"
        return
    fi
    if [ -x "${project_dir}/venv/bin/python" ]; then
        printf '%s\n' "${project_dir}/venv/bin"
        return
    fi
    echo "Virtualenv not found (.venv or venv)." >&2
    exit 1
}

resolve_runtime_binaries() {
    local project_dir="$1"
    VENV_BIN_DIR="$(resolve_virtualenv_bin_dir "${project_dir}")"
    PYTHON_BIN="${VENV_BIN_DIR}/python"
    if [ -x "${VENV_BIN_DIR}/uvicorn" ]; then
        UVICORN_LAUNCHER="${VENV_BIN_DIR}/uvicorn"
        UVICORN_USE_MODULE=0
    else
        UVICORN_LAUNCHER="${PYTHON_BIN}"
        UVICORN_USE_MODULE=1
    fi
}

print_command() {
    local first=1
    for arg in "$@"; do
        if [ "${first}" -eq 1 ]; then
            printf '%s' "${arg}"
            first=0
        else
            printf ' %s' "${arg}"
        fi
    done
    printf '\n'
}
