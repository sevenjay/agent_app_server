#!/usr/bin/env bash
set -Eeuo pipefail

readonly SERVICE_NAME="agent-app-server"
readonly UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
temp_unit=""

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

cleanup() {
    if [[ -n "$temp_unit" ]]; then
        rm -f -- "$temp_unit"
    fi
}

usage() {
    cat <<'USAGE'
Usage: sudo scripts/register_systemd.sh

Write /etc/systemd/system/agent-app-server.service, reload systemd, and enable
the service. The service is not started or restarted automatically.

Environment variables:
  AGENT_APP_SERVER_APP_DIR       Project directory. Default: repository root
  AGENT_APP_SERVER_SERVICE_USER  Linux user that runs the service. Default:
                                 SUDO_USER or the owner of the project directory
USAGE
}

resolve_repo_dir() {
    local script_dir
    script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
    cd "${script_dir}/.." >/dev/null 2>&1 || die "failed to resolve repository root"
    pwd -P
}

systemd_quote() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    printf '"%s"' "$value"
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

    [[ $EUID -eq 0 ]] || die "root privileges are required; run: sudo scripts/register_systemd.sh"
    command -v getent >/dev/null 2>&1 || die "missing required command: getent"
    command -v install >/dev/null 2>&1 || die "missing required command: install"
    command -v systemctl >/dev/null 2>&1 || die "missing required command: systemctl"

    local app_dir
    local app_owner
    local run_script
    local service_home
    local service_user

    app_dir="${AGENT_APP_SERVER_APP_DIR:-$(resolve_repo_dir)}"
    [[ -d "$app_dir" ]] || die "project directory not found: $app_dir"
    app_dir="$(cd "$app_dir" >/dev/null 2>&1 && pwd -P)"
    [[ "$app_dir" != *[[:space:]]* ]] || die "systemd project path must not contain whitespace: $app_dir"
    [[ -f "${app_dir}/pyproject.toml" ]] || die "pyproject.toml not found in project directory: $app_dir"

    run_script="${app_dir}/scripts/run.sh"
    [[ -x "$run_script" ]] || die "run script is not executable: $run_script"

    app_owner="$(stat -c '%U' "$app_dir")"
    service_user="${AGENT_APP_SERVER_SERVICE_USER:-${SUDO_USER:-$app_owner}}"
    getent passwd "$service_user" >/dev/null || die "service user does not exist: $service_user"
    service_home="$(getent passwd "$service_user" | cut -d: -f6)"
    [[ -n "$service_home" ]] || die "home directory is not configured for service user: $service_user"

    temp_unit="$(mktemp)"
    trap cleanup EXIT

    cat >"$temp_unit" <<EOF
[Unit]
Description=agent-app-server
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${app_dir}
ExecStart=$(systemd_quote "$run_script")

Restart=always
RestartSec=5

User=${service_user}
Environment=PYTHONUNBUFFERED=1
Environment=ENV_FOR_DYNACONF=production
Environment=$(systemd_quote "HOME=${service_home}")

KillSignal=SIGTERM
KillMode=control-group
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

    install -m 0644 "$temp_unit" "$UNIT_PATH"
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}.service"

    log "Installed and enabled ${UNIT_PATH}"
    log "Start it with: systemctl start ${SERVICE_NAME}.service"
}

main "$@"
