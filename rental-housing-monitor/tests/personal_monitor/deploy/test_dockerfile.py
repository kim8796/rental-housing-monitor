from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "Dockerfile"
ENTRYPOINT = ROOT / "deploy" / "entrypoint.sh"
SQUID_DOCKERFILE = ROOT / "deploy" / "squid.Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _between(text: str, start: str, end: str | None = None) -> str:
    if start not in text:
        return ""
    tail = text[text.index(start) :]
    if end is not None and end in tail:
        return tail[: tail.index(end)]
    return tail


def test_runtime_uses_exact_python_and_node_patch_images() -> None:
    dockerfile = _text(DOCKERFILE)
    python_images = re.findall(r"^FROM python:([^\s]+)", dockerfile, re.MULTILINE)
    node_images = re.findall(r"^FROM node:([^\s]+)", dockerfile, re.MULTILINE)
    assert len(python_images) == 2
    assert all(re.fullmatch(r"3\.12\.\d+-slim-bookworm", tag) for tag in python_images)
    assert node_images and re.fullmatch(r"22\.\d+\.\d+-bookworm-slim", node_images[0])
    assert "latest" not in dockerfile


def test_builder_copies_only_wheel_inputs_and_final_installs_wheels() -> None:
    dockerfile = _text(DOCKERFILE)
    copy_sources = re.findall(r"^COPY(?: --[^\s]+=[^\s]+)? ([^\n]+)$", dockerfile, re.MULTILINE)
    assert any(line == "pyproject.toml /build/pyproject.toml" for line in copy_sources)
    assert any(line == "src/ /build/src/" for line in copy_sources)
    assert "python -m build --wheel" in dockerfile
    assert "pip wheel" in dockerfile
    assert "--no-index" in dockerfile
    assert "--find-links=/wheelhouse" in dockerfile
    assert "COPY . " not in dockerfile
    for forbidden in (".env", ".git", "tests", "data/", "logs/", "vault", "auth.json"):
        assert all(forbidden not in line for line in copy_sources)


def test_runtime_installs_only_required_system_packages() -> None:
    dockerfile = _text(DOCKERFILE)
    assert "apt-get install -y --no-install-recommends" in dockerfile
    for package in (
        "age",
        "sqlite3",
        "curl",
        "ca-certificates",
        "xvfb",
        "x11vnc",
        "novnc",
        "websockify",
    ):
        assert re.search(rf"\b{re.escape(package)}\b", dockerfile)
    assert "COPY --from=node-runtime /usr/local/" in dockerfile
    assert "curl |" not in dockerfile
    assert "| sh" not in dockerfile
    assert "| bash" not in dockerfile


def test_runtime_pins_codex_and_installs_scrapling_browser_cache() -> None:
    dockerfile = _text(DOCKERFILE)
    assert "npm install --global @openai/codex@0.144.1" in dockerfile
    assert "PLAYWRIGHT_BROWSERS_PATH=/opt/personal-monitor/browsers" in dockerfile
    assert "scrapling install --force" in dockerfile
    assert "chmod -R a+rX /opt/personal-monitor/browsers" in dockerfile


def test_runtime_user_and_private_paths_are_fixed() -> None:
    dockerfile = _text(DOCKERFILE)
    assert "--gid 10001" in dockerfile
    assert "--uid 10001" in dockerfile
    for directory in (
        "db",
        "adaptive",
        "vault",
        "diagnostics",
        "logs",
        "codex-home",
    ):
        assert f"/srv/personal-monitor/{directory}" in dockerfile
    assert "/run/personal-monitor-ai" in dockerfile
    assert "chmod 0700" in dockerfile
    assert "chown -R 10001:10001" in dockerfile
    assert "USER 10001:10001" in dockerfile


def test_entrypoint_is_root_owned_nonwritable_and_default_is_safe() -> None:
    dockerfile = _text(DOCKERFILE)
    assert (
        "COPY --chown=0:0 --chmod=0555 deploy/entrypoint.sh "
        "/usr/local/bin/personal-monitor-entrypoint" in dockerfile
    )
    assert 'ENTRYPOINT ["/usr/local/bin/personal-monitor-entrypoint"]' in dockerfile
    assert 'CMD ["personal-monitor", "--help"]' in dockerfile


def test_image_contains_no_secret_or_public_port_instruction() -> None:
    dockerfile = _text(DOCKERFILE)
    assert "OPENAI_API_KEY" not in dockerfile
    assert "CODEX_API_KEY" not in dockerfile
    assert "TELEGRAM" not in dockerfile
    assert "DATA_GO" not in dockerfile
    assert not re.search(r"(?m)^EXPOSE\b", dockerfile)


def test_entrypoint_has_safe_shell_baseline() -> None:
    entrypoint = _text(ENTRYPOINT)
    assert entrypoint.startswith("#!/bin/sh\nset -eu\numask 077\n")
    assert "set -x" not in entrypoint
    assert "env\n" not in entrypoint
    assert "printenv" not in entrypoint
    assert "$*" not in entrypoint
    assert "chmod -R" not in entrypoint
    assert "chown -R" not in entrypoint


def test_serve_mode_validates_private_paths_and_initializes_database() -> None:
    entrypoint = _text(ENTRYPOINT)
    serve = _between(entrypoint, "serve)", "ai-worker)")
    for setting in (
        "PERSONAL_MONITOR_DATABASE_PATH",
        "PERSONAL_MONITOR_ADAPTIVE_ROOT",
        "PERSONAL_MONITOR_DIAGNOSTICS_ROOT",
        "PERSONAL_MONITOR_LOG_PATH",
        "PERSONAL_MONITOR_BACKUP_STATUS_PATH",
        "PERSONAL_MONITOR_MASTER_KEY_PATH",
        "PERSONAL_MONITOR_PROFILES_ROOT",
        "PERSONAL_MONITOR_CODEX_SOCKET",
        "PERSONAL_MONITOR_EGRESS_PROXY",
    ):
        assert setting in serve
    assert "readlink" in serve
    assert '"$(stat -c %u "$path")" = "10001"' in entrypoint
    assert '"$(stat -c %g "$path")" = "10001"' in entrypoint
    assert '"$(stat -c %a "$path")" = "$mode"' in entrypoint
    assert '"/srv/personal-monitor/db/monitor.db"' in serve
    assert '"$PERSONAL_MONITOR_PROFILES_ROOT/vault" "700"' in serve
    assert '"/srv/personal-monitor/logs/monitor.jsonl"' in serve
    assert '"/srv/personal-monitor/logs/backup-status.json"' in serve
    assert '"/run/personal-monitor-ai/worker.sock"' in serve
    assert '"http://egress-proxy:3128"' in serve
    assert "PERSONAL_MONITOR_LOG_ROOT" not in serve
    assert "wait_for_worker_socket" in serve
    assert 'validate_optional_regular "$PERSONAL_MONITOR_LOG_PATH" "600"' in serve
    assert 'validate_optional_regular "$PERSONAL_MONITOR_BACKUP_STATUS_PATH" "600"' in serve
    init = 'personal-monitor database init --path "$PERSONAL_MONITOR_DATABASE_PATH"'
    assert init in serve
    assert init in serve
    assert serve.index(init) < serve.index('exec "$@"')


def test_ai_worker_validates_only_its_private_directories() -> None:
    entrypoint = _text(ENTRYPOINT)
    worker = _between(entrypoint, "ai-worker)", "profile-bootstrap)")
    assert "PERSONAL_MONITOR_CODEX_HOME" in worker
    assert "PERSONAL_MONITOR_CODEX_TASK_ROOT" in worker
    assert "/run/personal-monitor-ai" in worker
    assert '"/run/personal-monitor-ai/worker.sock"' in worker
    assert "remove_stale_worker_socket" in worker
    assert 'exec "$@"' in worker
    for forbidden in (
        "DATABASE",
        "ADAPTIVE",
        "VAULT",
        "DIAGNOSTICS",
        "LOG",
        "TELEGRAM",
        "MASTER_KEY",
    ):
        assert forbidden not in worker


def test_worker_socket_wait_and_stale_cleanup_are_exact_and_bounded() -> None:
    entrypoint = _text(ENTRYPOINT)
    wait = _between(entrypoint, "wait_for_worker_socket()", "remove_stale_worker_socket()")
    remove = _between(entrypoint, "remove_stale_worker_socket()", "wait_for_display()")
    socket_validation = _between(entrypoint, "validate_socket()", "wait_for_worker_socket()")
    assert "socket_path=/run/personal-monitor-ai/worker.sock" in wait
    assert '"$attempts" -lt 100' in wait
    assert 'validate_socket "$socket_path"' in wait
    assert "socket_listener_ready" in wait
    assert 'before_identity=$(stat -c "%d:%i" "$socket_path")' in wait
    assert 'after_identity=$(stat -c "%d:%i" "$socket_path")' in wait
    assert '[ "$before_identity" = "$after_identity" ]' in wait
    assert wait.index("socket_listener_ready") < wait.index("return 0")
    assert "socket_path=/run/personal-monitor-ai/worker.sock" in remove
    assert 'validate_socket "$socket_path"' in remove
    assert "socket_listener_ready" in remove
    assert remove.index("socket_listener_ready") < remove.index('rm -f "$socket_path"')
    assert "fail" in remove[: remove.index('rm -f "$socket_path"')]
    assert 'rm -f "$socket_path"' in remove
    assert "*" not in remove
    assert '"$(stat -c %u "$path")" = "10001"' in socket_validation
    assert '"$(stat -c %g "$path")" = "10001"' in socket_validation
    assert '"$(stat -c %a "$path")" = "600"' in socket_validation


def test_profile_mode_contains_local_ui_lifecycle_and_exec() -> None:
    entrypoint = _text(ENTRYPOINT)
    profile = _between(entrypoint, "profile-bootstrap)")
    assert "PERSONAL_MONITOR_PROFILE_VAULT_ROOT" in profile
    assert "PERSONAL_MONITOR_PROFILE_TMPFS_ROOT" in profile
    assert "PERSONAL_MONITOR_MASTER_KEY_PATH" in profile
    assert "Xvfb :99" in profile
    assert "x11vnc" in profile
    assert "websockify" in profile
    assert "6080" in profile
    assert "trap" in profile
    assert "wait_for_display" in profile
    assert 'wait_for_tcp "5900"' in profile
    assert 'wait_for_tcp "6080"' in profile
    assert "kill -0" in entrypoint
    assert "BOOTSTRAP_PID" in profile
    assert "personal-monitor profile bootstrap" in profile
    assert '(exec "$@") <&0 &' in profile


def test_profile_readiness_and_cleanup_are_bounded_and_owner_checked() -> None:
    entrypoint = _text(ENTRYPOINT)
    display_wait = _between(entrypoint, "wait_for_display()", "tcp_ready()")
    cleanup = _between(entrypoint, "stop_process()", 'if [ "${1-}" = "personal-monitor" ]')
    assert '"$(stat -c %u "$display_socket")" = "10001"' in display_wait
    assert '"$(readlink -f "$display_socket")" = "$display_socket"' in display_wait
    assert "stop_process()" in entrypoint
    assert "kill -KILL" in cleanup
    assert "sleep 0.2" in cleanup


def test_entrypoint_has_valid_posix_shell_syntax() -> None:
    assert ENTRYPOINT.exists()
    checked = subprocess.run(
        ["sh", "-n", str(ENTRYPOINT)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stderr


def test_squid_image_is_exact_minimal_distribution_package() -> None:
    dockerfile = _text(SQUID_DOCKERFILE)
    assert dockerfile.startswith("FROM ubuntu:24.04\n")
    assert "apt-get install -y --no-install-recommends" in dockerfile
    assert "squid" in dockerfile
    assert "ca-certificates" in dockerfile
    assert "COPY deploy/squid.conf /etc/squid/squid.conf" in dockerfile
    assert "COPY . " not in dockerfile
    assert "EXPOSE" not in dockerfile


def test_dockerignore_excludes_secret_data_and_local_artifacts() -> None:
    dockerignore = _text(DOCKERIGNORE)
    for pattern in (
        ".git",
        ".github",
        ".env",
        ".env.*",
        "!.env.example",
        "data",
        "logs",
        "*.db*",
        "*.db-wal",
        "*.db-shm",
        "*.pem",
        "*.key",
        "auth.json",
        ".codex",
        "profiles",
        "vault",
        "*master.key*",
        "*identity*",
        ".venv",
        "__pycache__",
        ".worktrees",
        "tests",
        "docs",
        "*.tar*",
        "*.zip",
    ):
        assert pattern in dockerignore
