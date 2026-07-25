from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GCP = ROOT / "infra" / "gcp"
PREFLIGHT = GCP / "preflight.sh"
PROVISION = GCP / "provision.sh"
STARTUP = GCP / "startup.sh"
README = GCP / "README.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_task_six_files_exist() -> None:
    for path in (PREFLIGHT, PROVISION, STARTUP, README):
        assert path.is_file()


def test_preflight_is_strict_read_only_and_uses_fixed_scope() -> None:
    text = _text(PREFLIGHT)
    assert "set -Eeuo pipefail" in text
    assert 'PROJECT="local-social-native-wlk-0720"' in text
    assert 'REGION="asia-northeast3"' in text
    assert 'ZONES=("asia-northeast3-a" "asia-northeast3-b")' in text
    assert 'MACHINE_TYPE="e2-medium"' in text
    assert 'INSTANCE_NAME="personal-monitor-1"' in text
    for mutation in (
        " services enable ",
        " create ",
        " delete ",
        " update ",
        " add-iam-policy-binding ",
        " set-iam-policy ",
    ):
        assert mutation not in f" {text} "


def test_preflight_reports_closed_json_and_disabled_compute_unknowns() -> None:
    text = _text(PREFLIGHT)
    for key in (
        "schema_version",
        "project",
        "active_account",
        "billing_enabled",
        "budget_list_permitted",
        "billing_currency",
        "compute_api_enabled",
        "regional_cpu_quota",
        "machine_types",
        "vm_conflict",
        "cloud_run_services",
        "preflight_ready",
    ):
        assert key in text
    assert "unknown_until_enabled" in text
    assert "gcloud services list" in text
    assert "gcloud billing projects describe" in text
    assert "gcloud billing budgets list" in text
    assert "gcloud compute regions describe" in text
    assert "gcloud compute machine-types describe" in text
    assert "gcloud compute instances list" in text
    assert "gcloud run services list" in text


def test_provision_has_explicit_apply_gate_before_any_mutation() -> None:
    text = _text(PROVISION)
    gate = 'PERSONAL_MONITOR_PROVISION_CONFIRM'
    first_mutation = text.index("gcloud services enable")
    assert '[[ "${1-}" == "--apply" ]]' in text
    assert gate in text
    assert text.index(gate) < first_mutation
    assert "local-social-native-wlk-0720/personal-monitor-1" in text


def test_provision_never_touches_cloud_run_or_existing_service() -> None:
    text = _text(PROVISION)
    assert "gcloud run" not in text
    assert "local-social-api" not in text
    assert "run.googleapis.com" not in text


def test_budget_gate_precedes_compute_resource_mutations() -> None:
    text = _text(PROVISION)
    budget_gate = text.index("personal-monitor-monthly-50000")
    budget_currency = text.index("50000KRW")
    service_account = text.index("gcloud iam service-accounts create")
    firewall = text.index("gcloud compute firewall-rules create")
    instance = text.index("gcloud compute instances create")
    assert budget_gate < service_account
    assert budget_currency < service_account
    assert budget_gate < firewall < instance
    assert "--filter-projects=" in text
    assert "--calendar-period=month" in text
    for threshold in ("0.5", "0.8", "1.0"):
        assert f"percent={threshold},basis=current-spend" in text


def test_provision_is_describe_then_create_and_uses_exact_resources() -> None:
    text = _text(PROVISION)
    for describe, create in (
        (
            "gcloud iam service-accounts describe",
            "gcloud iam service-accounts create",
        ),
        ("gcloud storage buckets describe", "gcloud storage buckets create"),
        (
            "gcloud compute firewall-rules describe",
            "gcloud compute firewall-rules create",
        ),
        ("gcloud compute instances list", "gcloud compute instances create"),
    ):
        assert text.index(describe) < text.index(create)
    assert "personal-monitor-vm@" in text
    assert "gs://local-social-native-wlk-0720-personal-monitor-backups" in text
    assert "roles/storage.objectUser" in text
    assert "deploy/gcs-lifecycle.json" in text


def test_bucket_iam_binding_retries_bounded_service_account_propagation() -> None:
    text = _text(PROVISION)
    assert "bind_bucket_object_user" in text
    assert "for attempt in 1 2 3 4 5 6" in text
    assert '[[ "$attempt" -lt 6 ]] || return 1' in text
    assert "sleep 5" in text
    assert text.index("gcloud iam service-accounts create") < text.rindex(
        "bind_bucket_object_user || fail"
    )


def test_firewall_and_vm_policy_are_exact() -> None:
    text = _text(PROVISION)
    for value in (
        "35.235.240.0/20",
        "--priority=900",
        "--allow=tcp:22",
        "--priority=1000",
        "--action=deny",
        "--rules=all",
        "--source-ranges=0.0.0.0/0",
        "--target-tags=personal-monitor-iap",
        "--machine-type=e2-medium",
        "--boot-disk-size=50GB",
        "--boot-disk-type=pd-balanced",
        "--image-family=ubuntu-2404-lts-amd64",
        "--image-project=ubuntu-os-cloud",
        "enable-oslogin=TRUE",
        "--metadata-from-file=startup-script=",
        "--scopes=https://www.googleapis.com/auth/cloud-platform",
    ):
        assert value in text


def test_provision_prepares_billing_export_dataset_and_least_privilege_roles() -> None:
    text = _text(PROVISION)
    assert "bigquery.googleapis.com" in text
    assert 'BILLING_DATASET="billing_monitor"' in text
    assert "--location=US" in text
    assert "roles/bigquery.jobUser" in text
    assert "gcloud projects add-iam-policy-binding" in text
    assert '"role": "READER"' in text
    assert '"userByEmail": sys.argv[4]' in text
    assert "--update_mode=UPDATE_ACL" in text
    assert 'bq --project_id="$PROJECT" update --dataset' in text
    assert "add-iam-policy-binding --dataset" not in text


def test_existing_firewall_rules_are_described_as_json_and_validated() -> None:
    text = _text(PROVISION)
    assert "validate_firewall" in text
    assert "allowed,denied,sourceRanges,targetTags,priority,direction,network,disabled" in text
    assert 'rule_kind == "allow"' in text
    assert 'rule_kind == "deny"' in text


def test_startup_prepares_host_without_credentials_or_app_start() -> None:
    text = _text(STARTUP)
    assert "set -Eeuo pipefail" in text
    assert "docker.io" in text
    assert "docker-compose-v2" in text
    assert "age" in text
    assert "sqlite3" in text
    assert "/snap/bin" in text
    assert "command -v gcloud" in text
    assert "google-cloud-cli" not in text
    for directory in (
        "app",
        "db",
        "adaptive",
        "vault",
        "diagnostics",
        "logs",
        "backups",
    ):
        assert f"/srv/personal-monitor/{directory}" in text
    assert "/etc/personal-monitor" in text
    assert "10001:10001" in text
    assert "chmod 0700" in text
    for forbidden in (
        "git clone",
        "codex login",
        "auth.json",
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        ".env",
        "docker compose up",
        "personal-monitor.service",
    ):
        assert forbidden not in text


def test_readme_separates_code_generation_from_live_apply() -> None:
    text = _text(README)
    assert "preflight.sh" in text
    assert "읽기 전용" in text
    assert "provision.sh --apply" in text
    assert "PERSONAL_MONITOR_PROVISION_CONFIRM" in text
    assert "실행 체크포인트" in text
    assert "Cloud Run" in text


def test_lifecycle_file_remains_exact() -> None:
    lifecycle = json.loads((ROOT / "deploy" / "gcs-lifecycle.json").read_text())
    assert lifecycle == {
        "rule": [
            {
                "action": {"type": "Delete"},
                "condition": {"age": 8, "matchesPrefix": ["daily/"]},
            },
            {
                "action": {"type": "Delete"},
                "condition": {"age": 29, "matchesPrefix": ["weekly/"]},
            },
        ]
    }


def test_shell_syntax() -> None:
    for script in (PREFLIGHT, PROVISION, STARTUP):
        result = subprocess.run(
            ["bash", "-n", str(script)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
