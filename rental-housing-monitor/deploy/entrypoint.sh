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

validate_optional_regular() {
    path=$1
    mode=$2
    if [ -e "$path" ] || [ -L "$path" ]; then
        validate_regular "$path" "$mode"
    fi
}

validate_socket() {
    path=$1
    [ -S "$path" ] || fail
    [ ! -L "$path" ] || fail
    [ "$(readlink -f "$path")" = "$path" ] || fail
    [ "$(stat -c %u "$path")" = "10001" ] || fail
    [ "$(stat -c %g "$path")" = "10001" ] || fail
    [ "$(stat -c %a "$path")" = "600" ] || fail
}

socket_listener_ready() {
    /usr/local/bin/python -c \
        'import socket;s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.settimeout(0.1);s.connect("/run/personal-monitor-ai/worker.sock");s.close()' \
        >/dev/null 2>&1
}

wait_for_worker_socket() {
    socket_path=/run/personal-monitor-ai/worker.sock
    attempts=0
    while [ "$attempts" -lt 100 ]; do
        if [ -e "$socket_path" ] || [ -L "$socket_path" ]; then
            validate_socket "$socket_path"
            if ! before_identity=$(stat -c "%d:%i" "$socket_path"); then
                attempts=$((attempts + 1))
                sleep 0.1
                continue
            fi
            listener_ready=false
            if socket_listener_ready; then
                listener_ready=true
            fi
            if [ -e "$socket_path" ] || [ -L "$socket_path" ]; then
                validate_socket "$socket_path"
                if after_identity=$(stat -c "%d:%i" "$socket_path"); then
                    if [ "$listener_ready" = "true" ] &&
                        [ "$before_identity" = "$after_identity" ]; then
                        return 0
                    fi
                fi
            fi
        fi
        attempts=$((attempts + 1))
        sleep 0.1
    done
    fail
}

remove_stale_worker_socket() {
    socket_path=/run/personal-monitor-ai/worker.sock
    if [ -e "$socket_path" ] || [ -L "$socket_path" ]; then
        validate_socket "$socket_path"
        before_identity=$(stat -c "%d:%i" "$socket_path") || fail
        if socket_listener_ready; then
            validate_socket "$socket_path"
            after_identity=$(stat -c "%d:%i" "$socket_path") || fail
            [ "$before_identity" = "$after_identity" ] || fail
            fail
        fi
        if [ ! -e "$socket_path" ] && [ ! -L "$socket_path" ]; then
            return 0
        fi
        validate_socket "$socket_path"
        after_identity=$(stat -c "%d:%i" "$socket_path") || fail
        [ "$before_identity" = "$after_identity" ] || fail
        if socket_listener_ready; then
            fail
        fi
        validate_socket "$socket_path"
        after_identity=$(stat -c "%d:%i" "$socket_path") || fail
        [ "$before_identity" = "$after_identity" ] || fail
        rm -f "$socket_path"
        [ ! -e "$socket_path" ] && [ ! -L "$socket_path" ] || fail
    fi
}

wait_for_display() {
    display_socket=$1
    process_id=$2
    attempts=0
    while [ "$attempts" -lt 100 ]; do
        kill -0 "$process_id" 2>/dev/null || fail
        if [ -e "$display_socket" ] || [ -L "$display_socket" ]; then
            [ -S "$display_socket" ] || fail
            [ ! -L "$display_socket" ] || fail
            [ "$(readlink -f "$display_socket")" = "$display_socket" ] || fail
            [ "$(stat -c %u "$display_socket")" = "10001" ] || fail
            return 0
        fi
        attempts=$((attempts + 1))
        sleep 0.1
    done
    fail
}

tcp_ready() {
    /usr/local/bin/python -c \
        'import socket,sys;s=socket.socket();s.settimeout(0.1);s.connect(("127.0.0.1",int(sys.argv[1])));s.close()' \
        "$1" >/dev/null 2>&1
}

wait_for_tcp() {
    port=$1
    process_id=$2
    attempts=0
    while [ "$attempts" -lt 100 ]; do
        kill -0 "$process_id" 2>/dev/null || fail
        if tcp_ready "$port"; then
            return 0
        fi
        attempts=$((attempts + 1))
        sleep 0.1
    done
    fail
}

stop_process() {
    process_id=$1
    [ -n "$process_id" ] || return 0
    kill "$process_id" 2>/dev/null || true
    sleep 0.2
    kill -KILL "$process_id" 2>/dev/null || true
    wait "$process_id" 2>/dev/null || true
}

if [ "${1-}" = "personal-monitor" ]; then
    case "${2-}" in
        serve)
            require_fixed "${PERSONAL_MONITOR_DATABASE_PATH-}" \
                "/srv/personal-monitor/db/monitor.db"
            require_fixed "${PERSONAL_MONITOR_ADAPTIVE_ROOT-}" \
                "/srv/personal-monitor/adaptive"
            require_fixed "${PERSONAL_MONITOR_DIAGNOSTICS_ROOT-}" \
                "/srv/personal-monitor/diagnostics"
            require_fixed "${PERSONAL_MONITOR_LOG_PATH-}" \
                "/srv/personal-monitor/logs/monitor.jsonl"
            require_fixed "${PERSONAL_MONITOR_BACKUP_STATUS_PATH-}" \
                "/srv/personal-monitor/logs/backup-status.json"
            require_fixed "${PERSONAL_MONITOR_MASTER_KEY_PATH-}" \
                "/etc/personal-monitor/master.key"
            require_fixed "${PERSONAL_MONITOR_PROFILES_ROOT-}" \
                "/run/personal-monitor-profiles"
            require_fixed "${PERSONAL_MONITOR_CODEX_SOCKET-}" \
                "/run/personal-monitor-ai/worker.sock"
            require_fixed "${PERSONAL_MONITOR_EGRESS_PROXY-}" \
                "http://egress-proxy:3128"

            database_parent=$(dirname "$PERSONAL_MONITOR_DATABASE_PATH")
            [ "$(readlink -f "$database_parent")" = "/srv/personal-monitor/db" ] ||
                fail
            log_parent=$(dirname "$PERSONAL_MONITOR_LOG_PATH")
            [ "$(readlink -f "$log_parent")" = "/srv/personal-monitor/logs" ] ||
                fail
            backup_parent=$(dirname "$PERSONAL_MONITOR_BACKUP_STATUS_PATH")
            [ "$(readlink -f "$backup_parent")" = "/srv/personal-monitor/logs" ] ||
                fail

            validate_directory "/srv/personal-monitor/db" "700"
            validate_directory "$PERSONAL_MONITOR_ADAPTIVE_ROOT" "700"
            validate_directory "$PERSONAL_MONITOR_DIAGNOSTICS_ROOT" "700"
            validate_directory "/srv/personal-monitor/logs" "700"
            validate_directory "$PERSONAL_MONITOR_PROFILES_ROOT" "700"
            validate_directory "$PERSONAL_MONITOR_PROFILES_ROOT/vault" "700"
            validate_directory "/run/personal-monitor-ai" "700"
            validate_regular "$PERSONAL_MONITOR_MASTER_KEY_PATH" "600"
            validate_optional_regular "$PERSONAL_MONITOR_DATABASE_PATH" "600"
            validate_optional_regular "$PERSONAL_MONITOR_LOG_PATH" "600"
            validate_optional_regular "$PERSONAL_MONITOR_BACKUP_STATUS_PATH" "600"
            wait_for_worker_socket

            personal-monitor database init --path "$PERSONAL_MONITOR_DATABASE_PATH"
            exec "$@"
            ;;
        ai-worker)
            [ "$#" -eq 4 ] || fail
            require_fixed "${3-}" "--socket"
            require_fixed "${4-}" "/run/personal-monitor-ai/worker.sock"
            require_fixed "${PERSONAL_MONITOR_CODEX_HOME-}" \
                "/srv/personal-monitor/codex-home"
            require_fixed "${PERSONAL_MONITOR_CODEX_TASK_ROOT-}" "/work"
            validate_directory "$PERSONAL_MONITOR_CODEX_HOME" "700"
            validate_directory "$PERSONAL_MONITOR_CODEX_TASK_ROOT" "700"
            validate_directory "/run/personal-monitor-ai" "700"
            remove_stale_worker_socket
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

        BOOTSTRAP_PID=
        NOVNC_PID=
        VNC_PID=
        XVFB_PID=
        cleanup() {
            trap - EXIT HUP INT TERM
            for process_id in \
                "${BOOTSTRAP_PID-}" \
                "${NOVNC_PID-}" \
                "${VNC_PID-}" \
                "${XVFB_PID-}"
            do
                stop_process "$process_id"
            done
        }
        trap cleanup EXIT
        trap 'exit 129' HUP
        trap 'exit 130' INT
        trap 'exit 143' TERM

        Xvfb :99 -screen 0 1440x900x24 -nolisten tcp >/dev/null 2>&1 &
        XVFB_PID=$!
        wait_for_display "/tmp/.X11-unix/X99" "$XVFB_PID"
        x11vnc -display :99 -localhost -forever -shared -nopw >/dev/null 2>&1 &
        VNC_PID=$!
        wait_for_tcp "5900" "$VNC_PID"
        websockify --web=/usr/share/novnc/ 6080 localhost:5900 >/dev/null 2>&1 &
        NOVNC_PID=$!
        wait_for_tcp "6080" "$NOVNC_PID"
        export DISPLAY=:99

        set -- personal-monitor profile bootstrap "$@" \
            --profiles-root "$PERSONAL_MONITOR_PROFILE_VAULT_ROOT"
        (exec "$@") <&0 &
        BOOTSTRAP_PID=$!
        if wait "$BOOTSTRAP_PID"; then
            bootstrap_status=0
        else
            bootstrap_status=$?
        fi
        BOOTSTRAP_PID=
        exit "$bootstrap_status"
        ;;
    *)
        exec "$@"
        ;;
esac
