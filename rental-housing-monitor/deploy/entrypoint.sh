#!/bin/sh
set -eu
umask 077

fail() {
    printf '%s\n' "personal monitor startup validation failed" >&2
    exit 1
}

require_fixed() {
    [ "$1" = "$2" ] || fail
}

validate_directory() {
    path=$1
    mode=$2
    [ -d "$path" ] || fail
    [ ! -L "$path" ] || fail
    [ "$(readlink -f "$path")" = "$path" ] || fail
    [ "$(stat -c %u "$path")" = "10001" ] || fail
    [ "$(stat -c %g "$path")" = "10001" ] || fail
    [ "$(stat -c %a "$path")" = "$mode" ] || fail
}

validate_regular() {
    path=$1
    mode=$2
    [ -f "$path" ] || fail
    [ ! -L "$path" ] || fail
    [ "$(readlink -f "$path")" = "$path" ] || fail
    [ "$(stat -c %u "$path")" = "10001" ] || fail
    [ "$(stat -c %g "$path")" = "10001" ] || fail
    [ "$(stat -c %a "$path")" = "$mode" ] || fail
}

if [ "${1-}" = "personal-monitor" ]; then
    case "${2-}" in
        serve)
            require_fixed "${PERSONAL_MONITOR_ADAPTIVE_ROOT-}" \
                "/srv/personal-monitor/adaptive"
            require_fixed "${PERSONAL_MONITOR_DIAGNOSTICS_ROOT-}" \
                "/srv/personal-monitor/diagnostics"
            require_fixed "${PERSONAL_MONITOR_LOG_ROOT-}" \
                "/srv/personal-monitor/logs"
            require_fixed "${PERSONAL_MONITOR_MASTER_KEY_PATH-}" \
                "/etc/personal-monitor/master.key"
            require_fixed "${PERSONAL_MONITOR_PROFILES_ROOT-}" \
                "/run/personal-monitor-profiles"

            case "${PERSONAL_MONITOR_DATABASE_PATH-}" in
                "/srv/personal-monitor/db/"*) ;;
                *) fail ;;
            esac
            database_parent=$(dirname "$PERSONAL_MONITOR_DATABASE_PATH")
            [ "$(readlink -f "$database_parent")" = "/srv/personal-monitor/db" ] ||
                fail

            validate_directory "/srv/personal-monitor/db" "700"
            validate_directory "$PERSONAL_MONITOR_ADAPTIVE_ROOT" "700"
            validate_directory "$PERSONAL_MONITOR_DIAGNOSTICS_ROOT" "700"
            validate_directory "$PERSONAL_MONITOR_LOG_ROOT" "700"
            validate_directory "$PERSONAL_MONITOR_PROFILES_ROOT" "700"
            validate_directory "$PERSONAL_MONITOR_PROFILES_ROOT/vault" "700"
            validate_directory "/run/personal-monitor-ai" "700"
            validate_regular "$PERSONAL_MONITOR_MASTER_KEY_PATH" "600"

            if [ -e "$PERSONAL_MONITOR_DATABASE_PATH" ] ||
                [ -L "$PERSONAL_MONITOR_DATABASE_PATH" ]; then
                validate_regular "$PERSONAL_MONITOR_DATABASE_PATH" "600"
            fi

            personal-monitor database init --path "$PERSONAL_MONITOR_DATABASE_PATH"
            exec "$@"
            ;;
        ai-worker)
            require_fixed "${PERSONAL_MONITOR_CODEX_HOME-}" \
                "/srv/personal-monitor/codex-home"
            require_fixed "${PERSONAL_MONITOR_CODEX_TASK_ROOT-}" "/work"
            validate_directory "$PERSONAL_MONITOR_CODEX_HOME" "700"
            validate_directory "$PERSONAL_MONITOR_CODEX_TASK_ROOT" "700"
            validate_directory "/run/personal-monitor-ai" "700"
            exec "$@"
            ;;
        *)
            exec "$@"
            ;;
    esac
fi

case "${1-}" in
    profile-bootstrap)
        shift
        require_fixed "${PERSONAL_MONITOR_PROFILE_VAULT_ROOT-}" \
            "/srv/personal-monitor/profile-bootstrap"
        require_fixed "${PERSONAL_MONITOR_PROFILE_TMPFS_ROOT-}" \
            "/run/personal-monitor-profiles"
        require_fixed "${PERSONAL_MONITOR_MASTER_KEY_PATH-}" \
            "/srv/personal-monitor/profile-bootstrap/master.key"
        validate_directory "$PERSONAL_MONITOR_PROFILE_VAULT_ROOT" "700"
        validate_directory "$PERSONAL_MONITOR_PROFILE_VAULT_ROOT/vault" "700"
        validate_directory "$PERSONAL_MONITOR_PROFILE_TMPFS_ROOT" "700"
        validate_regular "$PERSONAL_MONITOR_MASTER_KEY_PATH" "600"

        cleanup() {
            trap - EXIT HUP INT TERM
            kill "${NOVNC_PID-}" "${VNC_PID-}" "${XVFB_PID-}" 2>/dev/null || true
        }
        trap cleanup EXIT HUP INT TERM

        Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &
        XVFB_PID=$!
        x11vnc -display :99 -localhost -forever -shared -nopw >/dev/null 2>&1 &
        VNC_PID=$!
        websockify --web=/usr/share/novnc/ 6080 localhost:5900 >/dev/null 2>&1 &
        NOVNC_PID=$!
        export DISPLAY=:99

        set -- personal-monitor profile bootstrap "$@" \
            --profiles-root "$PERSONAL_MONITOR_PROFILE_VAULT_ROOT"
        exec "$@"
        ;;
    *)
        exec "$@"
        ;;
esac
