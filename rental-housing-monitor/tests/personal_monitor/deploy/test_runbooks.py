from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SYSTEMD = ROOT / "deploy" / "systemd"
OPERATIONS = ROOT / "docs" / "operations"
COMPOSE_WRAPPER = ROOT / "deploy" / "personal-monitor-compose"
MAIN_SERVICE = SYSTEMD / "personal-monitor.service"
BACKUP_SERVICE = SYSTEMD / "personal-monitor-backup.service"
BACKUP_TIMER = SYSTEMD / "personal-monitor-backup.timer"
VERIFY_SERVICE = SYSTEMD / "personal-monitor-verify.service"
VERIFY_TIMER = SYSTEMD / "personal-monitor-verify.timer"
GCP_DEPLOY = OPERATIONS / "gcp-deploy.md"
BACKUP_RESTORE = OPERATIONS / "backup-restore.md"
RENTAL_CUTOVER = OPERATIONS / "rental-cutover.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_required_units_and_runbooks_exist() -> None:
    for path in (
        COMPOSE_WRAPPER,
        MAIN_SERVICE,
        BACKUP_SERVICE,
        BACKUP_TIMER,
        VERIFY_SERVICE,
        VERIFY_TIMER,
        GCP_DEPLOY,
        BACKUP_RESTORE,
        RENTAL_CUTOVER,
    ):
        assert path.is_file()


def test_compose_wrapper_enters_private_app_directory_as_root() -> None:
    text = _text(COMPOSE_WRAPPER)
    assert text.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail\n")
    assert "cd /srv/personal-monitor/app" in text
    assert (
        'exec docker compose --env-file /srv/personal-monitor/.env "$@"'
        in text
    )
    assert "$*" not in text
    result = subprocess.run(
        ["bash", "-n", COMPOSE_WRAPPER],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_main_service_owns_foreground_compose_lifecycle() -> None:
    text = _text(MAIN_SERVICE)
    for value in (
        "Wants=network-online.target",
        "Requires=docker.service",
        "After=network-online.target docker.service",
        "WorkingDirectory=/srv/personal-monitor/app",
        "docker compose --env-file /srv/personal-monitor/.env up --remove-orphans",
        "docker compose --env-file /srv/personal-monitor/.env stop --timeout 90",
        "Restart=on-failure",
        "TimeoutStopSec=120",
    ):
        assert value in text
    assert " up -d" not in text


def test_backup_timer_is_daily_kst_with_persistent_catchup() -> None:
    service = _text(BACKUP_SERVICE)
    timer = _text(BACKUP_TIMER)
    assert "EnvironmentFile=/srv/personal-monitor/.env" in service
    assert (
        "ExecStart=/srv/personal-monitor/app/scripts/backup_personal_monitor.sh"
        in service
    )
    assert "UMask=0077" in service
    assert "OnCalendar=*-*-* 03:10:00 Asia/Seoul" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=0" in timer


def test_verify_timer_reuses_pre_encryption_restore_smoke_path() -> None:
    service = _text(VERIFY_SERVICE)
    timer = _text(VERIFY_TIMER)
    assert (
        "ExecStart=/srv/personal-monitor/app/scripts/backup_personal_monitor.sh"
        in service
    )
    assert "OnCalendar=Sun *-*-* 04:10:00 Asia/Seoul" in timer
    assert "Persistent=true" in timer
    assert "AGE_IDENTITY" not in service
    assert "age --decrypt" not in service


def test_deploy_runbook_keeps_secrets_out_of_git_and_uses_chatgpt_login() -> None:
    text = _text(GCP_DEPLOY)
    for value in (
        "gcloud compute ssh personal-monitor-1",
        "--tunnel-through-iap",
        "sudoedit /srv/personal-monitor/.env",
        "chmod 0600 /srv/personal-monitor/.env",
        "openssl rand 32",
        "chown 10001:10001 /etc/personal-monitor/master.key",
        "AGE_RECIPIENT",
        "PERSONAL_MONITOR_BACKUP_BUCKET",
        "personal-monitor-compose run --rm --no-deps --entrypoint codex",
        "codex login --device-auth",
        "codex login status",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "systemctl enable --now personal-monitor.service",
        "systemctl enable --now personal-monitor-backup.timer",
        "systemctl enable --now personal-monitor-verify.timer",
    ):
        assert value in text
    assert "Git/Codex 채팅" in text
    assert "제거" in text
    assert text.count("-e CODEX_HOME=/srv/personal-monitor/codex-home") >= 3
    assert text.count("-e HOME=/srv/personal-monitor/codex-home") >= 3


def test_deploy_runbook_hands_private_source_archive_to_service_uid() -> None:
    text = _text(GCP_DEPLOY)
    repository_root = 'git -C "$(git rev-parse --show-toplevel)" archive'
    chown = "sudo chown 10001:10001 /tmp/personal-monitor-src.tar.gz"
    chmod = "sudo chmod 0600 /tmp/personal-monitor-src.tar.gz"
    extract = (
        "sudo -u personal-monitor sh -c "
        "'umask 022; exec tar -xzf /tmp/personal-monitor-src.tar.gz"
    )
    verify_compose = (
        """sudo test "$(sudo stat -c '%a %u:%g' """
        """/srv/personal-monitor/app/compose.yaml)" = "644 10001:10001\""""
    )
    assert repository_root in text
    assert "HEAD:rental-housing-monitor" in text
    assert chown in text
    assert chmod in text
    assert verify_compose in text
    assert text.index(chown) < text.index(chmod) < text.index(extract) < text.index(verify_compose)


def test_runbooks_use_root_compose_wrapper_for_private_app_directory() -> None:
    deploy = _text(GCP_DEPLOY)
    cutover = _text(RENTAL_CUTOVER)
    assert (
        "sudo install -o root -g root -m 0755 "
        "/srv/personal-monitor/app/deploy/personal-monitor-compose "
        "/usr/local/sbin/personal-monitor-compose"
        in deploy
    )
    for text in (deploy, cutover):
        assert "cd /srv/personal-monitor/app" not in text
        assert "sudo docker compose" not in text
        assert "sudo personal-monitor-compose" in text


def test_device_login_uses_one_shot_container_before_worker_start() -> None:
    text = _text(GCP_DEPLOY)
    login = text.index("codex login --device-auth")
    worker_start = text.index("sudo personal-monitor-compose up -d codex-worker")
    assert (
        "sudo personal-monitor-compose run --rm --no-deps "
        "--entrypoint codex"
        in text
    )
    assert login < worker_start
    assert "up -d --build codex-worker" not in text


def test_deploy_runbook_has_separate_health_checks() -> None:
    text = _text(GCP_DEPLOY)
    for value in (
        "personal-monitor-compose ps",
        "personal-monitor database integrity-check",
        "health_write_probe",
        "next_run_at",
        "df -h /srv/personal-monitor",
        "gcloud storage objects list",
        "telegram_poll",
        "codex login status",
        "backup-status.json",
    ):
        assert value in text


def test_deploy_runbook_configures_and_seeds_gcp_billing_monitor() -> None:
    text = _text(GCP_DEPLOY)
    for value in (
        "PERSONAL_MONITOR_BILLING_PROJECT_ID=local-social-native-wlk-0720",
        "PERSONAL_MONITOR_BILLING_DATASET_ID=billing_monitor",
        "PERSONAL_MONITOR_BILLING_MAXIMUM_BYTES=100000000",
        "Cloud Billing Standard usage export",
        "personal-monitor billing register-credit",
        "--original-won 460418.00",
        "--remaining-won 455145.36",
        "--starts-on 2026-07-08",
        "--ends-on 2026-10-08",
    ):
        assert value in text


def test_restore_runbook_uses_off_server_identity_and_empty_target() -> None:
    text = _text(BACKUP_RESTORE)
    for value in (
        "gcloud storage cp",
        "gcloud storage objects describe",
        "sha256sum",
        "scripts/restore_personal_monitor.sh",
        "chmod 0700",
        "빈 디렉터리",
        "오프서버",
        "PRAGMA integrity_check",
        "/srv/personal-monitor/logs/backup-status.json",
    ):
        assert value in text
    assert "라이브 데이터 디렉터리를 대상으로 지정하지" in text


def test_cutover_runbook_has_seven_day_shadow_and_exact_rollback() -> None:
    text = _text(RENTAL_CUTOVER)
    for value in (
        "origin/data",
        "origin/data:rental-housing-monitor/data/announcements.db",
        'git -C "$(git rev-parse --show-toplevel)" fetch origin data',
        "migration import-rental",
        "--dry-run",
        "--owner",
        "telegram-user:",
        "--target telegram-main",
        "--target-address",
        "PERSONAL_MONITOR_TELEGRAM_DELIVERY_CHAT_ID",
        "migration shadow-run",
        "NullDeliverySender",
        "migration duplicate-probe",
        "migration status",
        '"cutover_ready":true',
        "rental-housing-monitor-daily",
        "Pause",
        "isPaused=true",
        "run-once",
        "--delivery enabled",
        "systemctl start personal-monitor.service",
        "systemctl stop personal-monitor.service",
        "Resume",
        "isPaused=false",
        ".github/workflows/rental-housing-monitor.yml",
        "data 브랜치",
    ):
        assert value in text
    for day in range(1, 8):
        assert f"Day {day}" in text
    assert "삭제하지" in text


def test_all_referenced_repository_paths_exist() -> None:
    for relative in (
        "compose.yaml",
        "deploy/personal-monitor-compose",
        "scripts/backup_personal_monitor.sh",
        "scripts/restore_personal_monitor.sh",
        "scripts/verify_backup.sh",
        "deploy/systemd/personal-monitor.service",
        "deploy/systemd/personal-monitor-backup.service",
        "deploy/systemd/personal-monitor-backup.timer",
        "deploy/systemd/personal-monitor-verify.service",
        "deploy/systemd/personal-monitor-verify.timer",
        "docs/operations/gcp-deploy.md",
        "docs/operations/backup-restore.md",
        "docs/operations/rental-cutover.md",
    ):
        assert (ROOT / relative).exists(), relative


def test_documented_personal_monitor_command_groups_parse_help() -> None:
    commands = (
        ("database", "init"),
        ("database", "integrity-check"),
        ("billing", "register-credit"),
        ("migration", "import-rental"),
        ("migration", "shadow-run"),
        ("migration", "duplicate-probe"),
        ("migration", "status"),
        ("run-once",),
    )
    for command in commands:
        result = subprocess.run(
            [sys.executable, "-m", "personal_monitor", *command, "--help"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (command, result.stderr)
        assert "usage: personal-monitor" in result.stdout


def test_readme_links_all_operations_runbooks() -> None:
    text = _text(ROOT / "README.md")
    for relative in (
        "docs/operations/gcp-deploy.md",
        "docs/operations/backup-restore.md",
        "docs/operations/rental-cutover.md",
    ):
        assert relative in text


def test_runbooks_contain_no_literal_credentials() -> None:
    combined = "\n".join(
        _text(path) for path in (GCP_DEPLOY, BACKUP_RESTORE, RENTAL_CUTOVER)
    )
    for pattern in (
        r"\b[0-9]{8,10}:[A-Za-z0-9_-]{30,}\b",
        r"\bsk-[A-Za-z0-9_-]{20,}\b",
        r"\bghp_[A-Za-z0-9]{20,}\b",
        r"\bage1[023456789acdefghjklmnpqrstuvwxyz]{40,}\b",
    ):
        assert re.search(pattern, combined) is None
