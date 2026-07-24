#!/usr/bin/env bash
set -Eeuo pipefail
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/homebrew/bin
export PATH
umask 077

PROJECT="local-social-native-wlk-0720"
REGION="asia-northeast3"
ZONES=("asia-northeast3-a" "asia-northeast3-b")
MACHINE_TYPE="e2-medium"
INSTANCE_NAME="personal-monitor-1"

fail() {
    printf '%s\n' "personal monitor GCP preflight failed" >&2
    exit 2
}

for command_name in gcloud grep mktemp python3 rm; do
    command -v "$command_name" >/dev/null 2>&1 || fail
done

WORKSPACE=$(mktemp -d "${TMPDIR:-/tmp}/personal-monitor-preflight.XXXXXXXXXX")
cleanup() {
    rm -rf -- "$WORKSPACE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

ACTIVE_ACCOUNT=$(gcloud auth list \
    --filter=status:ACTIVE \
    --format='value(account)' 2>/dev/null || true)
CONFIGURED_PROJECT=$(gcloud config get-value project 2>/dev/null || true)

BILLING_FILE="$WORKSPACE/billing.json"
if ! gcloud billing projects describe "$PROJECT" \
    --format='json(billingAccountName,billingEnabled,projectId)' \
    >"$BILLING_FILE" 2>/dev/null; then
    printf '%s' '{}' >"$BILLING_FILE"
fi

BILLING_ACCOUNT=$(python3 - "$BILLING_FILE" <<'PY'
import json
import sys
from pathlib import Path

try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    name = value.get("billingAccountName", "")
except (OSError, json.JSONDecodeError, AttributeError):
    name = ""
if isinstance(name, str) and name.startswith("billingAccounts/"):
    print(name.removeprefix("billingAccounts/"))
PY
)

BUDGETS_FILE="$WORKSPACE/budgets.json"
BUDGET_LIST_PERMITTED=false
if [[ -n "$BILLING_ACCOUNT" ]] &&
    gcloud billing budgets list \
        --billing-account="$BILLING_ACCOUNT" \
        --format='json(name,displayName,amount)' \
        >"$BUDGETS_FILE" 2>/dev/null; then
    BUDGET_LIST_PERMITTED=true
else
    printf '%s' '[]' >"$BUDGETS_FILE"
fi

SERVICES_FILE="$WORKSPACE/services"
gcloud services list \
    --enabled \
    --project="$PROJECT" \
    --filter='config.name=compute.googleapis.com' \
    --format='value(config.name)' >"$SERVICES_FILE" 2>/dev/null || true
COMPUTE_API_ENABLED=false
if grep -Fxq "compute.googleapis.com" "$SERVICES_FILE"; then
    COMPUTE_API_ENABLED=true
fi

RUN_SERVICES_FILE="$WORKSPACE/cloud-run-services"
gcloud run services list \
    --project="$PROJECT" \
    --platform=managed \
    --format='value(metadata.name)' >"$RUN_SERVICES_FILE" 2>/dev/null || true

REGION_FILE="$WORKSPACE/region.json"
INSTANCES_FILE="$WORKSPACE/instances.json"
MACHINE_FILES=()
if [[ "$COMPUTE_API_ENABLED" == true ]]; then
    gcloud compute regions describe "$REGION" \
        --project="$PROJECT" \
        --format='json(quotas)' >"$REGION_FILE" 2>/dev/null || printf '%s' '{}' >"$REGION_FILE"
    gcloud compute instances list \
        --project="$PROJECT" \
        --filter="name=$INSTANCE_NAME" \
        --format='json(name,zone)' >"$INSTANCES_FILE" 2>/dev/null ||
        printf '%s' '[]' >"$INSTANCES_FILE"
    for zone in "${ZONES[@]}"; do
        machine_file="$WORKSPACE/machine-${zone}.json"
        if ! gcloud compute machine-types describe "$MACHINE_TYPE" \
            --zone="$zone" \
            --project="$PROJECT" \
            --format='json(name,guestCpus,memoryMb)' >"$machine_file" 2>/dev/null; then
            printf '%s' '{}' >"$machine_file"
        fi
        MACHINE_FILES+=("$machine_file")
    done
else
    printf '%s' '{}' >"$REGION_FILE"
    printf '%s' '[]' >"$INSTANCES_FILE"
    for zone in "${ZONES[@]}"; do
        machine_file="$WORKSPACE/machine-${zone}.json"
        printf '%s' '{}' >"$machine_file"
        MACHINE_FILES+=("$machine_file")
    done
fi

if python3 - \
    "$PROJECT" \
    "$ACTIVE_ACCOUNT" \
    "$CONFIGURED_PROJECT" \
    "$BILLING_FILE" \
    "$BUDGET_LIST_PERMITTED" \
    "$BUDGETS_FILE" \
    "$COMPUTE_API_ENABLED" \
    "$REGION_FILE" \
    "$INSTANCES_FILE" \
    "$RUN_SERVICES_FILE" \
    "${ZONES[0]}" "${MACHINE_FILES[0]}" \
    "${ZONES[1]}" "${MACHINE_FILES[1]}" <<'PY'
import json
import sys
from pathlib import Path


def load_json(path: str, fallback):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return fallback


(
    project,
    active_account,
    configured_project,
    billing_path,
    budget_permitted_text,
    budgets_path,
    compute_enabled_text,
    region_path,
    instances_path,
    run_services_path,
    zone_a,
    machine_a_path,
    zone_b,
    machine_b_path,
) = sys.argv[1:]

account_valid = (
    bool(active_account)
    and "\n" not in active_account
    and configured_project == project
)
billing = load_json(billing_path, {})
billing_enabled = (
    isinstance(billing, dict)
    and billing.get("projectId") == project
    and billing.get("billingEnabled") is True
    and isinstance(billing.get("billingAccountName"), str)
)
budget_list_permitted = budget_permitted_text == "true"
budgets = load_json(budgets_path, [])
currencies = set()
if isinstance(budgets, list):
    for budget in budgets:
        if not isinstance(budget, dict):
            continue
        amount = budget.get("amount", {})
        specified = amount.get("specifiedAmount", {}) if isinstance(amount, dict) else {}
        currency = specified.get("currencyCode") if isinstance(specified, dict) else None
        if isinstance(currency, str) and currency:
            currencies.add(currency)
billing_currency = next(iter(currencies)) if len(currencies) == 1 else (
    "unknown_no_budget" if not currencies else "conflicting"
)

compute_api_enabled = compute_enabled_text == "true"
if compute_api_enabled:
    region = load_json(region_path, {})
    quota_candidates = []
    if isinstance(region, dict) and isinstance(region.get("quotas"), list):
        for quota in region["quotas"]:
            if not isinstance(quota, dict) or quota.get("metric") not in {"CPUS", "E2_CPUS"}:
                continue
            limit = quota.get("limit")
            usage = quota.get("usage")
            if isinstance(limit, (int, float)) and isinstance(usage, (int, float)):
                quota_candidates.append((quota["metric"], limit - usage))
    if quota_candidates:
        metric, available = max(quota_candidates, key=lambda value: value[1])
        regional_cpu_quota = {
            "status": "available" if available >= 2 else "insufficient",
            "metric": metric,
            "available": available,
        }
    else:
        regional_cpu_quota = {"status": "unavailable", "metric": None, "available": None}

    machine_types = {}
    for zone, path in ((zone_a, machine_a_path), (zone_b, machine_b_path)):
        machine = load_json(path, {})
        machine_types[zone] = (
            "available"
            if isinstance(machine, dict)
            and machine.get("name") == "e2-medium"
            and machine.get("guestCpus") == 2
            else "unavailable"
        )
    instances = load_json(instances_path, [])
    vm_conflict = bool(instances) if isinstance(instances, list) else True
else:
    regional_cpu_quota = {
        "status": "unknown_until_enabled",
        "metric": None,
        "available": None,
    }
    machine_types = {
        zone_a: "unknown_until_enabled",
        zone_b: "unknown_until_enabled",
    }
    vm_conflict = "unknown_until_enabled"

try:
    cloud_run_services = sorted(
        {
            line.strip()
            for line in Path(run_services_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    )
except (OSError, UnicodeError):
    cloud_run_services = []

preflight_ready = (
    account_valid
    and billing_enabled
    and budget_list_permitted
    and billing_currency == "KRW"
    and compute_api_enabled
    and regional_cpu_quota["status"] == "available"
    and "available" in machine_types.values()
    and vm_conflict is False
)
result = {
    "schema_version": 1,
    "project": project,
    "active_account": active_account,
    "account_valid": account_valid,
    "billing_enabled": billing_enabled,
    "budget_list_permitted": budget_list_permitted,
    "billing_currency": billing_currency,
    "compute_api_enabled": compute_api_enabled,
    "regional_cpu_quota": regional_cpu_quota,
    "machine_types": machine_types,
    "vm_conflict": vm_conflict,
    "cloud_run_services": cloud_run_services,
    "preflight_ready": preflight_ready,
}
print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
raise SystemExit(0 if preflight_ready else 1)
PY
then
    exit 0
fi
exit 1
