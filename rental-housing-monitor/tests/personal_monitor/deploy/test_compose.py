from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "compose.yaml"
SQUID = ROOT / "deploy" / "squid.conf"
SERVICE = ROOT / "src" / "personal_monitor" / "service.py"
CONFIG = ROOT / "src" / "personal_monitor" / "config.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _service_block(name: str) -> str:
    compose = _text(COMPOSE)
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-z][a-z0-9-]*:\n|^[a-z])",
        compose,
    )
    return match.group(0) if match else ""


def _compose_command(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ROOT / ".env.example"),
            *args,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _require_compose_cli() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI unavailable")
    probe = subprocess.run(
        ["docker", "compose", "version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip("Docker Compose CLI unavailable")


def test_compose_renders_with_exact_services_and_admin_profile() -> None:
    _require_compose_cli()
    rendered = _compose_command("--profile", "admin", "config", "--format", "json")
    assert rendered.returncode == 0, rendered.stderr
    assert "${" not in rendered.stdout
    model = json.loads(rendered.stdout)
    assert set(model["services"]) == {
        "monitor",
        "codex-worker",
        "egress-proxy",
        "profile-bootstrap",
    }
    assert model["services"]["profile-bootstrap"]["profiles"] == ["admin"]


def test_default_compose_services_exclude_profile_bootstrap() -> None:
    _require_compose_cli()
    services = _compose_command("config", "--services")
    assert services.returncode == 0, services.stderr
    assert set(services.stdout.split()) == {
        "monitor",
        "codex-worker",
        "egress-proxy",
    }


def test_compose_uses_only_explicit_private_networks() -> None:
    compose = _text(COMPOSE)
    monitor = _service_block("monitor")
    worker = _service_block("codex-worker")
    proxy = _service_block("egress-proxy")
    profile = _service_block("profile-bootstrap")
    assert re.search(r"(?m)^networks:\n", compose)
    assert "worker-egress:" in compose
    assert "monitor-egress:" in compose
    assert "internal: true" not in compose
    assert "monitor-egress" in monitor and "worker-egress" not in monitor
    assert "worker-egress" in worker and "monitor-egress" not in worker
    assert "monitor-egress" in proxy and "worker-egress" not in proxy
    assert "monitor-egress" in profile and "worker-egress" not in profile
    assert "10.77.0.0/24" in compose
    assert "10.78.0.0/24" in compose


def test_monitor_has_explicit_mount_and_environment_allowlists() -> None:
    monitor = _service_block("monitor")
    for mount in (
        "/srv/personal-monitor/db",
        "/srv/personal-monitor/adaptive",
        "/srv/personal-monitor/vault:/run/personal-monitor-profiles/vault",
        "/srv/personal-monitor/diagnostics",
        "/srv/personal-monitor/logs",
        "/etc/personal-monitor/master.key:ro",
        "ai-socket:/run/personal-monitor-ai",
    ):
        assert mount in monitor
    for setting in (
        "PERSONAL_MONITOR_DATABASE_PATH=/srv/personal-monitor/db/monitor.db",
        "PERSONAL_MONITOR_ADAPTIVE_ROOT=/srv/personal-monitor/adaptive",
        "PERSONAL_MONITOR_DIAGNOSTICS_ROOT=/srv/personal-monitor/diagnostics",
        "PERSONAL_MONITOR_LOG_PATH=/srv/personal-monitor/logs/monitor.jsonl",
        ("PERSONAL_MONITOR_BACKUP_STATUS_PATH=/srv/personal-monitor/logs/backup-status.json"),
        "PERSONAL_MONITOR_MASTER_KEY_PATH=/etc/personal-monitor/master.key",
        "PERSONAL_MONITOR_CODEX_SOCKET=/run/personal-monitor-ai/worker.sock",
        "PERSONAL_MONITOR_EGRESS_PROXY=http://egress-proxy:3128",
        "PERSONAL_MONITOR_TIMEZONE=Asia/Seoul",
        "${PERSONAL_MONITOR_DATA_GO_KR_SERVICE_KEY}",
        "${PERSONAL_MONITOR_TELEGRAM_BOT_TOKEN}",
    ):
        assert setting in monitor
    for forbidden in (
        "DATA_GO_KR_SERVICE_KEY=",
        "TELEGRAM_BOT_TOKEN=",
        "TELEGRAM_CHAT_ID=",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "env_file:",
        "docker.sock",
    ):
        assert forbidden not in monitor.replace(
            "PERSONAL_MONITOR_DATA_GO_KR_SERVICE_KEY=", ""
        ).replace("PERSONAL_MONITOR_TELEGRAM_BOT_TOKEN=", "")


def test_monitor_persists_runtime_vault_and_exposes_backup_status() -> None:
    monitor = _service_block("monitor")
    service = _text(SERVICE)
    config = _text(CONFIG)
    assert 'settings.profiles_root / "vault"' in service
    assert "/srv/personal-monitor/vault:/run/personal-monitor-profiles/vault" in monitor
    assert '"/srv/personal-monitor/backup-status.json"' in config
    assert (
        "PERSONAL_MONITOR_BACKUP_STATUS_PATH="
        "/srv/personal-monitor/logs/backup-status.json" in monitor
    )


def test_worker_has_only_codex_socket_mounts_and_allowlisted_environment() -> None:
    worker = _service_block("codex-worker")
    assert "codex-home:/srv/personal-monitor/codex-home" in worker
    assert "ai-socket:/run/personal-monitor-ai" in worker
    for forbidden in (
        "/srv/personal-monitor/db",
        "/srv/personal-monitor/adaptive",
        "/srv/personal-monitor/vault",
        "/srv/personal-monitor/diagnostics",
        "/srv/personal-monitor/logs",
        "master.key",
        "profiles",
        "docker.sock",
        "TELEGRAM",
        "DATA_GO",
        "DATABASE",
        "EGRESS_PROXY",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "env_file:",
    ):
        assert forbidden not in worker
    assert "PERSONAL_MONITOR_CODEX_BINARY=codex" in worker
    assert "PERSONAL_MONITOR_CODEX_HOME=/srv/personal-monitor/codex-home" in worker
    assert "PERSONAL_MONITOR_CODEX_TASK_ROOT=/work" in worker
    assert "PERSONAL_MONITOR_NODE_BINARY=${PERSONAL_MONITOR_NODE_BINARY:-node}" in worker


def test_monitor_and_worker_share_private_socket_without_tcp() -> None:
    monitor = _service_block("monitor")
    worker = _service_block("codex-worker")
    assert "ai-socket:/run/personal-monitor-ai" in monitor
    assert "ai-socket:/run/personal-monitor-ai" in worker
    assert "worker.sock" in monitor
    assert "worker.sock" in worker
    assert "depends_on:" in monitor
    assert "codex-worker:" in monitor
    assert "condition: service_started" in monitor
    assert "ports:" not in monitor
    assert "ports:" not in worker


@pytest.mark.parametrize(
    ("service_name", "command", "restart"),
    [
        ("monitor", "command: [personal-monitor, serve]", "restart: unless-stopped"),
        (
            "codex-worker",
            "command: [personal-monitor, ai-worker, --socket, "
            "/run/personal-monitor-ai/worker.sock]",
            "restart: unless-stopped",
        ),
    ],
)
def test_application_services_are_locked_down(
    service_name: str, command: str, restart: str
) -> None:
    service = _service_block(service_name)
    assert 'user: "10001:10001"' in service
    assert command in service
    assert restart in service
    assert "read_only: true" in service
    assert "cap_drop:" in service and "- ALL" in service
    assert "no-new-privileges:true" in service
    assert "/tmp:rw,noexec,nosuid,nodev,mode=0700,uid=10001,gid=10001" in service


def test_monitor_private_tmpfs_and_no_public_port() -> None:
    monitor = _service_block("monitor")
    assert (
        "/run/personal-monitor-profiles:"
        "rw,noexec,nosuid,nodev,mode=0700,uid=10001,gid=10001" in monitor
    )
    assert "ports:" not in monitor


def test_profile_bootstrap_is_admin_only_loopback_and_data_isolated() -> None:
    profile = _service_block("profile-bootstrap")
    assert 'profiles: ["admin"]' in profile
    assert 'restart: "no"' in profile
    assert 'user: "10001:10001"' in profile
    assert "entrypoint: [/usr/local/bin/personal-monitor-entrypoint, profile-bootstrap]" in profile
    assert "127.0.0.1:6080:6080" in profile
    assert "PERSONAL_MONITOR_EGRESS_PROXY=http://egress-proxy:3128" in profile
    assert "https://example.invalid/profile-bootstrap" in profile
    assert "profile-bootstrap-placeholder" in profile
    assert "monitor-egress" in profile and "worker-egress" not in profile
    for forbidden in (
        "/srv/personal-monitor/db",
        "/srv/personal-monitor/adaptive",
        "/srv/personal-monitor/logs",
        "codex-home",
        "ai-socket",
        "docker.sock",
        "TELEGRAM",
    ):
        assert forbidden not in profile


def test_only_admin_profile_publishes_a_loopback_port() -> None:
    compose = _text(COMPOSE)
    assert compose.count("ports:") == 1
    assert compose.count("127.0.0.1:6080:6080") == 1
    assert "0.0.0.0:" not in compose


def test_proxy_has_no_host_boundary_or_secrets() -> None:
    proxy = _service_block("egress-proxy")
    assert "context: ." in proxy
    assert "dockerfile: deploy/squid.Dockerfile" in proxy
    assert "restart: unless-stopped" in proxy
    assert "read_only: true" in proxy
    assert "cap_drop:" in proxy and "- ALL" in proxy
    assert "no-new-privileges:true" in proxy
    assert "ports:" not in proxy
    assert "volumes:" not in proxy
    assert "environment:" not in proxy
    assert "env_file:" not in proxy
    assert "/var/run/docker.sock" not in proxy


def test_no_service_mounts_host_or_ambient_configuration() -> None:
    compose = _text(COMPOSE)
    assert "\nversion:" not in f"\n{compose}"
    assert "env_file:" not in compose
    assert "/var/run/docker.sock" not in compose
    assert "- .:" not in compose
    assert ":/workspace" not in compose
    assert "/:/host" not in compose
    assert ".env:" not in compose


def test_squid_deny_policy_precedes_single_monitor_allow() -> None:
    squid = _text(SQUID)
    allow = "http_access allow monitor_network safe_ports"
    assert squid.count(allow) == 1
    allow_index = squid.index(allow)
    required_denies = (
        "acl metadata_hosts dstdomain",
        "acl metadata_ipv4 dst",
        "acl forbidden_ipv4 dst",
        "acl forbidden_ipv6 dst",
        "http_access deny metadata_hosts",
        "http_access deny metadata_ipv4",
        "http_access deny forbidden_ipv4",
        "http_access deny forbidden_ipv6",
    )
    for directive in required_denies:
        assert directive in squid
        assert squid.index(directive) < allow_index
    assert squid.index(allow) < squid.index("http_access deny all")


def test_squid_limits_ports_bodies_and_sensitive_logging() -> None:
    squid = _text(SQUID)
    assert "acl safe_ports port 80 443" in squid
    assert "http_access deny !safe_ports" in squid
    assert "request_header_access Accept-Encoding deny all" in squid
    assert "request_header_add Accept-Encoding identity" in squid
    assert "reply_body_max_size 10485760 allow all" in squid
    assert "cache deny all" in squid
    assert "cache_dir null /tmp" in squid
    assert "access_log none" in squid
    assert "cache_log /dev/null" in squid
    assert "http_port 3128" in squid


def test_squid_denies_metadata_private_reserved_and_ipv6_ranges() -> None:
    squid = _text(SQUID)
    for value in (
        "metadata.google.internal",
        "169.254.169.254/32",
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
        "2001:db8::/32",
    ):
        assert value in squid
