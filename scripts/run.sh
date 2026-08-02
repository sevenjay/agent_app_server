#!/usr/bin/env bash
set -Eeuo pipefail

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
    printf '[%s] ERROR: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2
    exit 1
}

on_error() {
    local exit_code=$?
    local line_no=${BASH_LINENO[0]:-${LINENO}}
    printf '[%s] ERROR: command failed with exit code %s at line %s: %s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$exit_code" "$line_no" "$BASH_COMMAND" >&2
    exit "$exit_code"
}

usage() {
    cat <<'USAGE'
Usage: scripts/run.sh

Start the agent-app-server service. This script does not fetch code, install
dependencies, create database backups, or apply migrations.

Environment variables:
  RUN_APP_DIR                Project directory. Default: repository root
  DEPLOY_APP_DIR             Fallback project directory when RUN_APP_DIR is unset
  POETRY_BIN=/path/to/poetry Optional Poetry executable path
  PYTHONUNBUFFERED           Default: 1
USAGE
}

resolve_poetry_bin() {
    local candidate
    local candidates=()

    if [[ -n "${POETRY_BIN:-}" ]]; then
        [[ -x "$POETRY_BIN" ]] || die "POETRY_BIN is not executable: $POETRY_BIN"
        printf '%s\n' "$POETRY_BIN"
        return
    fi

    if candidate="$(command -v poetry 2>/dev/null)"; then
        candidates+=("$candidate")
    fi

    candidates+=(
        "${HOME:-}/.local/bin/poetry"
        "/root/.local/bin/poetry"
        "/usr/local/bin/poetry"
        "/usr/bin/poetry"
        "/opt/poetry/bin/poetry"
    )

    if [[ -n "${SUDO_USER:-}" ]]; then
        candidates+=("/home/${SUDO_USER}/.local/bin/poetry")
    fi

    for candidate in "${candidates[@]}"; do
        if [[ -n "$candidate" && -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return
        fi
    done

    die "missing required command: poetry; set POETRY_BIN=/path/to/poetry or install Poetry in PATH"
}

resolve_repo_dir() {
    local script_dir
    script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
    cd "${script_dir}/.." >/dev/null 2>&1 || die "failed to resolve repository root"
    pwd -P
}

main() {
    case "${1:-}" in
        -h|--help)
            usage
            exit 0
            ;;
        "")
            ;;
        *)
            usage >&2
            die "unknown argument: $1"
            ;;
    esac

    trap on_error ERR

    local app_dir
    local poetry_bin

    app_dir="${RUN_APP_DIR:-${DEPLOY_APP_DIR:-$(resolve_repo_dir)}}"

    cd "$app_dir" || die "failed to enter project directory: $app_dir"
    [[ -f pyproject.toml ]] || die "pyproject.toml not found in project directory: $app_dir"

    poetry_bin="$(resolve_poetry_bin)"

    export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

    log "Starting agent-app-server service from $app_dir with ${poetry_bin}"
    exec "$poetry_bin" run python main.py
}

main "$@"
