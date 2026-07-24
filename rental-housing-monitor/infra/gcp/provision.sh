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
SERVICE_ACCOUNT_NAME="personal-monitor-vm"
SERVICE_ACCOUNT_EMAIL="personal-monitor-vm@local-social-native-wlk-0720.iam.gserviceaccount.com"
BUCKET="gs://local-social-native-wlk-0720-personal-monitor-backups"
BUDGET_NAME="personal-monitor-monthly-50000"
CONFIRM_EXPECTED="local-social-native-wlk-0720/personal-monitor-1"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPOSITORY_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd -P)
LIFECYCLE_FILE="$REPOSITORY_ROOT/deploy/gcs-lifecycle.json"
STARTUP_FILE="$SCRIPT_DIR/startup.sh"

fail() {
    printf '%s\n' "personal monitor GCP provisioning stopped" >&2
    exit 1
}

bind_bucket_object_user() {
    local attempt
    for attempt in 1 2 3 4 5 6; do
        if gcloud storage buckets add-iam-policy-binding "$BUCKET" \
            --member="serviceAccount:$SERVICE_ACCOUNT_EMAIL" \
            --role=roles/storage.objectUser \
            --quiet >/dev/null 2>&1; then
            return 0
        fi
        [[ "$attempt" -lt 6 ]] || return 1
        sleep 5
    done
    return 1
}

[[ "${1-}" == "--apply" ]] || fail
[[ $# -eq 1 ]] || fail
[[ "${PERSONAL_MONITOR_PROVISION_CONFIRM-}" == "$CONFIRM_EXPECTED" ]] || fail

for command_name in gcloud grep mktemp python3 rm sleep; do
    command -v "$command_name" >/dev/null 2>&1 || fail
done
[[ -f "$LIFECYCLE_FILE" && ! -L "$LIFECYCLE_FILE" ]] || fail
[[ -f "$STARTUP_FILE" && ! -L "$STARTUP_FILE" ]] || fail

WORKSPACE=$(mktemp -d "${TMPDIR:-/tmp}/personal-monitor-provision.XXXXXXXXXX")
cleanup() {
    rm -rf -- "$WORKSPACE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

ACTIVE_ACCOUNT=$(gcloud auth list \
    --filter=status:ACTIVE \
    --format='value(account)' 2>/dev/null)
CONFIGURED_PROJECT=$(gcloud config get-value project 2>/dev/null)
[[ -n "$ACTIVE_ACCOUNT" && "$ACTIVE_ACCOUNT" != *$'\n'* ]] || fail
[[ "$CONFIGURED_PROJECT" == "$PROJECT" ]] || fail

BILLING_FILE="$WORKSPACE/billing.json"
gcloud billing projects describe "$PROJECT" \
    --format='json(billingAccountName,billingEnabled,projectId)' \
    >"$BILLING_FILE" 2>/dev/null || fail
BILLING_ACCOUNT=$(python3 - "$BILLING_FILE" "$PROJECT" <<'PY'
import json
import sys
from pathlib import Path

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
name = value.get("billingAccountName")
if (
    value.get("projectId") != sys.argv[2]
    or value.get("billingEnabled") is not True
    or not isinstance(name, str)
    or not name.startswith("billingAccounts/")
):
    raise SystemExit(1)
print(name.removeprefix("billingAccounts/"))
PY
) || fail

# Budget creation is the first external mutation. Compute resources remain untouched
# if budget permissions or the billing account currency reject the KRW amount.
gcloud services enable billingbudgets.googleapis.com \
    --project="$PROJECT" --quiet >/dev/null 2>&1 || fail

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" \
    --format='value(projectNumber)' 2>/dev/null)
[[ "$PROJECT_NUMBER" =~ ^[0-9]+$ ]] || fail

validate_budget() {
    local budgets_file=$1
    python3 - "$budgets_file" "$BUDGET_NAME" "$PROJECT_NUMBER" <<'PY'
import json
import sys
from pathlib import Path

budgets = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(budgets, list):
    raise SystemExit(1)
matches = [
    budget for budget in budgets
    if isinstance(budget, dict) and budget.get("displayName") == sys.argv[2]
]
if not matches:
    raise SystemExit(10)
if len(matches) != 1:
    raise SystemExit(1)
budget = matches[0]
amount = budget.get("amount", {}).get("specifiedAmount", {})
budget_filter = budget.get("budgetFilter", {})
rules = budget.get("thresholdRules", [])
actual_rules = {
    (rule.get("thresholdPercent"), rule.get("spendBasis"))
    for rule in rules
    if isinstance(rule, dict)
}
if (
    amount.get("currencyCode") != "KRW"
    or str(amount.get("units")) != "50000"
    or amount.get("nanos", 0) != 0
    or budget_filter.get("calendarPeriod") != "MONTH"
    or budget_filter.get("projects") != [f"projects/{sys.argv[3]}"]
    or actual_rules
    != {
        (0.5, "CURRENT_SPEND"),
        (0.8, "CURRENT_SPEND"),
        (1.0, "CURRENT_SPEND"),
    }
):
    raise SystemExit(1)
PY
}

BUDGETS_FILE="$WORKSPACE/budgets.json"
gcloud billing budgets list \
    --billing-account="$BILLING_ACCOUNT" \
    --format='json(name,displayName,amount,budgetFilter,thresholdRules)' \
    >"$BUDGETS_FILE" 2>/dev/null || fail
if validate_budget "$BUDGETS_FILE"; then
    :
else
    budget_status=$?
    [[ "$budget_status" -eq 10 ]] || fail
    gcloud billing budgets create \
        --billing-account="$BILLING_ACCOUNT" \
        --display-name="$BUDGET_NAME" \
        --budget-amount=50000KRW \
        --filter-projects="projects/$PROJECT_NUMBER" \
        --calendar-period=month \
        --threshold-rule=percent=0.5,basis=current-spend \
        --threshold-rule=percent=0.8,basis=current-spend \
        --threshold-rule=percent=1.0,basis=current-spend \
        --quiet >/dev/null 2>&1 || fail
    gcloud billing budgets list \
        --billing-account="$BILLING_ACCOUNT" \
        --format='json(name,displayName,amount,budgetFilter,thresholdRules)' \
        >"$BUDGETS_FILE" 2>/dev/null || fail
    validate_budget "$BUDGETS_FILE" || fail
fi

gcloud services enable \
    compute.googleapis.com \
    iap.googleapis.com \
    storage.googleapis.com \
    --project="$PROJECT" --quiet >/dev/null 2>&1 || fail

REGION_FILE="$WORKSPACE/region.json"
gcloud compute regions describe "$REGION" \
    --project="$PROJECT" \
    --format='json(quotas)' >"$REGION_FILE" 2>/dev/null || fail
python3 - "$REGION_FILE" <<'PY' || exit 1
import json
import sys
from pathlib import Path

region = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
available = []
for quota in region.get("quotas", []):
    if quota.get("metric") not in {"CPUS", "E2_CPUS"}:
        continue
    limit = quota.get("limit")
    usage = quota.get("usage")
    if isinstance(limit, (int, float)) and isinstance(usage, (int, float)):
        available.append(limit - usage)
if not available or max(available) < 2:
    raise SystemExit(1)
PY

SELECTED_ZONE=
for zone in "${ZONES[@]}"; do
    if gcloud compute machine-types describe "$MACHINE_TYPE" \
        --zone="$zone" \
        --project="$PROJECT" \
        --format='value(name)' 2>/dev/null | grep -Fxq "$MACHINE_TYPE"; then
        SELECTED_ZONE=$zone
        break
    fi
done
[[ -n "$SELECTED_ZONE" ]] || fail
gcloud compute networks describe default \
    --project="$PROJECT" \
    --format='value(name)' 2>/dev/null | grep -Fxq default || fail

if ! gcloud iam service-accounts describe "$SERVICE_ACCOUNT_EMAIL" \
    --project="$PROJECT" --format='value(email)' 2>/dev/null |
    grep -Fxq "$SERVICE_ACCOUNT_EMAIL"; then
    gcloud iam service-accounts create "$SERVICE_ACCOUNT_NAME" \
        --project="$PROJECT" \
        --display-name="Personal monitor VM" \
        --quiet >/dev/null 2>&1 || fail
fi

BUCKET_FILE="$WORKSPACE/bucket.json"
if gcloud storage buckets describe "$BUCKET" \
    --raw \
    --format='json(location,iamConfiguration)' >"$BUCKET_FILE" 2>/dev/null; then
    python3 - "$BUCKET_FILE" <<'PY' || exit 1
import json
import sys
from pathlib import Path

bucket = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
uniform = bucket.get("iamConfiguration", {}).get("uniformBucketLevelAccess", {})
if bucket.get("location") != "ASIA-NORTHEAST3" or uniform.get("enabled") is not True:
    raise SystemExit(1)
PY
else
    gcloud storage buckets create "$BUCKET" \
        --project="$PROJECT" \
        --location="$REGION" \
        --uniform-bucket-level-access \
        --quiet >/dev/null 2>&1 || fail
fi
gcloud storage buckets update "$BUCKET" \
    --lifecycle-file="$LIFECYCLE_FILE" \
    --quiet >/dev/null 2>&1 || fail
bind_bucket_object_user || fail

ALLOW_FIREWALL="personal-monitor-iap-ssh"
DENY_FIREWALL="personal-monitor-deny-ingress"

validate_firewall() {
    local firewall_file=$1
    local rule_kind=$2
    python3 - "$firewall_file" "$rule_kind" <<'PY'
import json
import sys
from pathlib import Path

rule = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
rule_kind = sys.argv[2]
if (
    rule.get("direction") != "INGRESS"
    or rule.get("sourceRanges")
    != (["35.235.240.0/20"] if rule_kind == "allow" else ["0.0.0.0/0"])
    or rule.get("targetTags") != ["personal-monitor-iap"]
    or rule.get("priority") != (900 if rule_kind == "allow" else 1000)
    or not str(rule.get("network", "")).endswith("/global/networks/default")
    or rule.get("disabled", False) is not False
):
    raise SystemExit(1)
if rule_kind == "allow":
    if (
        rule.get("allowed") != [{"IPProtocol": "tcp", "ports": ["22"]}]
        or rule.get("denied", []) != []
    ):
        raise SystemExit(1)
elif rule_kind == "deny":
    if (
        rule.get("denied") != [{"IPProtocol": "all"}]
        or rule.get("allowed", []) != []
    ):
        raise SystemExit(1)
else:
    raise SystemExit(1)
PY
}

ALLOW_FIREWALL_FILE="$WORKSPACE/allow-firewall.json"
if ! gcloud compute firewall-rules describe "$ALLOW_FIREWALL" \
    --project="$PROJECT" \
    --format='json(allowed,denied,sourceRanges,targetTags,priority,direction,network,disabled)' \
    >"$ALLOW_FIREWALL_FILE" 2>/dev/null; then
    gcloud compute firewall-rules create "$ALLOW_FIREWALL" \
        --project="$PROJECT" \
        --network=default \
        --direction=INGRESS \
        --priority=900 \
        --allow=tcp:22 \
        --source-ranges=35.235.240.0/20 \
        --target-tags=personal-monitor-iap \
        --quiet >/dev/null 2>&1 || fail
    gcloud compute firewall-rules describe "$ALLOW_FIREWALL" \
        --project="$PROJECT" \
        --format='json(allowed,denied,sourceRanges,targetTags,priority,direction,network,disabled)' \
        >"$ALLOW_FIREWALL_FILE" 2>/dev/null || fail
fi
validate_firewall "$ALLOW_FIREWALL_FILE" allow || fail

DENY_FIREWALL_FILE="$WORKSPACE/deny-firewall.json"
if ! gcloud compute firewall-rules describe "$DENY_FIREWALL" \
    --project="$PROJECT" \
    --format='json(allowed,denied,sourceRanges,targetTags,priority,direction,network,disabled)' \
    >"$DENY_FIREWALL_FILE" 2>/dev/null; then
    gcloud compute firewall-rules create "$DENY_FIREWALL" \
        --project="$PROJECT" \
        --network=default \
        --direction=INGRESS \
        --priority=1000 \
        --action=deny \
        --rules=all \
        --source-ranges=0.0.0.0/0 \
        --target-tags=personal-monitor-iap \
        --quiet >/dev/null 2>&1 || fail
    gcloud compute firewall-rules describe "$DENY_FIREWALL" \
        --project="$PROJECT" \
        --format='json(allowed,denied,sourceRanges,targetTags,priority,direction,network,disabled)' \
        >"$DENY_FIREWALL_FILE" 2>/dev/null || fail
fi
validate_firewall "$DENY_FIREWALL_FILE" deny || fail

INSTANCES_FILE="$WORKSPACE/instances.json"
gcloud compute instances list \
    --project="$PROJECT" \
    --filter="name=$INSTANCE_NAME" \
    --format='json(name,zone,machineType,tags,serviceAccounts,metadata)' \
    >"$INSTANCES_FILE" 2>/dev/null || fail
EXISTING_ZONE=$(python3 - "$INSTANCES_FILE" "$SERVICE_ACCOUNT_EMAIL" <<'PY'
import json
import sys
from pathlib import Path

instances = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not instances:
    raise SystemExit(10)
if not isinstance(instances, list) or len(instances) != 1:
    raise SystemExit(1)
instance = instances[0]
zone = str(instance.get("zone", "")).rsplit("/", 1)[-1]
machine = str(instance.get("machineType", "")).rsplit("/", 1)[-1]
tags = instance.get("tags", {}).get("items", [])
accounts = instance.get("serviceAccounts", [])
metadata_items = instance.get("metadata", {}).get("items", [])
metadata = {
    item.get("key"): item.get("value")
    for item in metadata_items
    if isinstance(item, dict)
}
if (
    instance.get("name") != "personal-monitor-1"
    or zone not in {"asia-northeast3-a", "asia-northeast3-b"}
    or machine != "e2-medium"
    or tags != ["personal-monitor-iap"]
    or len(accounts) != 1
    or accounts[0].get("email") != sys.argv[2]
    or accounts[0].get("scopes")
    != ["https://www.googleapis.com/auth/devstorage.read_write"]
    or metadata.get("enable-oslogin") != "TRUE"
    or metadata.get("block-project-ssh-keys") != "TRUE"
):
    raise SystemExit(1)
print(zone)
PY
) || instance_status=$?

if [[ -n "$EXISTING_ZONE" ]]; then
    DISK_FILE="$WORKSPACE/disk.json"
    gcloud compute disks describe "$INSTANCE_NAME" \
        --zone="$EXISTING_ZONE" \
        --project="$PROJECT" \
        --format='json(name,sizeGb,type)' >"$DISK_FILE" 2>/dev/null || fail
    python3 - "$DISK_FILE" <<'PY' || exit 1
import json
import sys
from pathlib import Path

disk = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if (
    disk.get("name") != "personal-monitor-1"
    or str(disk.get("sizeGb")) != "50"
    or not str(disk.get("type", "")).endswith("/pd-balanced")
):
    raise SystemExit(1)
PY
    printf '%s\n' "personal monitor GCP resources already match"
    exit 0
fi
[[ "${instance_status-}" -eq 10 ]] || fail

gcloud compute instances create "$INSTANCE_NAME" \
    --project="$PROJECT" \
    --zone="$SELECTED_ZONE" \
    --machine-type=e2-medium \
    --network=default \
    --tags=personal-monitor-iap \
    --service-account="$SERVICE_ACCOUNT_EMAIL" \
    --scopes=https://www.googleapis.com/auth/devstorage.read_write \
    --boot-disk-size=50GB \
    --boot-disk-type=pd-balanced \
    --image-family=ubuntu-2404-lts-amd64 \
    --image-project=ubuntu-os-cloud \
    --metadata=enable-oslogin=TRUE,block-project-ssh-keys=TRUE \
    --metadata-from-file=startup-script="$STARTUP_FILE" \
    --shielded-secure-boot \
    --shielded-vtpm \
    --shielded-integrity-monitoring \
    --quiet >/dev/null 2>&1 || fail

printf '%s\n' "personal monitor GCP resources provisioned"
