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
Usage: scripts/deploy.sh

Deploy the configured git branch, install runtime dependencies, create a
consistent SQLite backup, and apply database migrations. This script does not
start main.py.

Environment variables:
  DEPLOY_REMOTE              Git remote to fetch from. Default: origin
  DEPLOY_BRANCH              Git branch to deploy. Default: master
  DEPLOY_APP_DIR             Project directory. Default: repository root
  DEPLOY_ALLOW_DIRTY=1       Allow reset when tracked files have local changes
  DEPLOY_SKIP_GIT=1          Skip fetch/reset
  DEPLOY_SKIP_INSTALL=1      Skip poetry install
  DEPLOY_SKIP_MIGRATIONS=1   Skip alembic upgrade/current
  POETRY_BIN=/path/to/poetry Optional Poetry executable path
  DATABASE_URL=...           Optional database URL used by Alembic
USAGE
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
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

ensure_clean_tracked_worktree() {
    if [[ "${DEPLOY_ALLOW_DIRTY:-0}" == "1" ]]; then
        log "DEPLOY_ALLOW_DIRTY=1; skipping tracked worktree cleanliness check"
        return
    fi

    if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
        git status --short --untracked-files=no >&2
        die "tracked files have local changes; commit/stash them or rerun with DEPLOY_ALLOW_DIRTY=1"
    fi
}

deploy_git_ref() {
    local remote=$1
    local branch=$2
    local target_ref="${remote}/${branch}"
    local before_rev
    local after_rev

    require_command git

    git remote get-url "$remote" >/dev/null 2>&1 || die "git remote not found: $remote"
    before_rev="$(git rev-parse --short HEAD)"

    ensure_clean_tracked_worktree

    log "Fetching ${remote}/${branch}"
    git fetch --prune "$remote" "+refs/heads/${branch}:refs/remotes/${remote}/${branch}"

    log "Resetting worktree to ${target_ref}"
    git reset --hard "$target_ref"

    after_rev="$(git rev-parse --short HEAD)"
    log "Checked out ${after_rev} (was ${before_rev})"
}

install_dependencies() {
    local poetry_bin

    if [[ "${DEPLOY_SKIP_INSTALL:-0}" == "1" ]]; then
        log "DEPLOY_SKIP_INSTALL=1; skipping poetry install"
        return
    fi

    poetry_bin="$(resolve_poetry_bin)"

    log "Installing runtime dependencies with ${poetry_bin}"
    "$poetry_bin" install --only main --no-root --no-interaction
}

run_migrations() {
    local poetry_bin

    if [[ "${DEPLOY_SKIP_MIGRATIONS:-0}" == "1" ]]; then
        log "DEPLOY_SKIP_MIGRATIONS=1; skipping Alembic migrations"
        return
    fi

    poetry_bin="$(resolve_poetry_bin)"

    log "Creating a consistent pre-migration SQLite backup"
    "$poetry_bin" run python -m scripts.backup_database

    log "Applying database migrations"
    "$poetry_bin" run alembic upgrade head

    log "Current database migration revision"
    "$poetry_bin" run alembic current
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
    local remote
    local branch

    app_dir="${DEPLOY_APP_DIR:-$(resolve_repo_dir)}"
    remote="${DEPLOY_REMOTE:-origin}"
    branch="${DEPLOY_BRANCH:-master}"

    cd "$app_dir" || die "failed to enter project directory: $app_dir"
    [[ -d .git ]] || die "project directory is not a git worktree: $app_dir"
    [[ -f pyproject.toml ]] || die "pyproject.toml not found in project directory: $app_dir"

    log "Project directory: $app_dir"

    if [[ "${DEPLOY_SKIP_GIT:-0}" == "1" ]]; then
        log "DEPLOY_SKIP_GIT=1; skipping git fetch/reset"
    else
        deploy_git_ref "$remote" "$branch"
    fi

    install_dependencies
    run_migrations
}

main "$@"
