# Rental Migration and GCP Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy the personal monitor on a separate Google Compute Engine VM and migrate the existing LH/SH/GH monitor after seven consecutive shadow matches and an idempotent state import.

**Architecture:** Existing collectors are wrapped as one allowlisted Python adapter while the GitHub Actions/QStash path stays authoritative. Docker Compose isolates the application and Codex worker; encrypted GCS backups and host scripts remain provider-portable; cutover pauses then eventually deletes only the old QStash schedule.

**Tech Stack:** Python 3.12+, existing LH/SH/GH collectors, Docker Compose, Codex CLI 0.144.1, Scrapling 0.4.11, Ubuntu 24.04, Google Compute Engine, GCS, IAP SSH, OS Login, age, systemd.

## Global Constraints

- Do not alter or stop `local-social-api` Cloud Run, its Artifact Registry, database, or S3 configuration.
- Keep QStash schedule `rental-housing-monitor-daily` and `.github/workflows/rental-housing-monitor.yml` active throughout implementation and shadow mode.
- Preserve LH official JSON API usage; do not replace it with Scrapling.
- Preserve existing SH/GH parsing until separate fixture and shadow parity proves a replacement.
- Use fixed monitor ID `rental-housing-seoul-gyeonggi`, adapter reference `rental_housing`, and schedule `13 12 * * *` in `Asia/Seoul` with zero jitter.
- Preserve institution isolation and send “오늘은 신규 공고가 없습니다.” only when LH, SH, and GH all succeed.
- Require seven consecutive Asia/Seoul dates with matching IDs, filters, institution status, and no unresolved difference before cutover.
- Keep the old data branch and workflow for rollback; do not delete either in this plan.
- Deploy in project `local-social-native-wlk-0720`, zone `asia-northeast3-a` or `asia-northeast3-b`, on `e2-medium` with 50 GB `pd-balanced` and Ubuntu 24.04 x86-64.
- Permit no public application port; permit SSH only from IAP range `35.235.240.0/20` and explicitly deny all other ingress to the VM tag.
- Keep application data under `/srv/personal-monitor` and use bind mounts so backup/restore is independent of Docker volume internals.
- Encrypt every backup with the operator's age public key; keep the age private key off the VM.
- Retain `daily/` backup objects eight days and `weekly/` objects 29 days, producing seven daily and four weekly restore points in steady state.

---

### Task 1: Wrap the existing rental monitor as an allowlisted Python adapter

**Files:**
- Create: `rental-housing-monitor/src/personal_monitor/adapters/rental_housing.py`
- Modify: `rental-housing-monitor/src/personal_monitor/adapters/registry.py`
- Create: `rental-housing-monitor/tests/personal_monitor/adapters/test_rental_housing.py`
- Reuse unchanged: `rental-housing-monitor/tests/fixtures/lh_notices.json`
- Reuse unchanged: `rental-housing-monitor/tests/fixtures/sh_list.html`
- Reuse unchanged: `rental-housing-monitor/tests/fixtures/sh_detail.html`
- Reuse unchanged: `rental-housing-monitor/tests/fixtures/gh_rental_list.html`
- Reuse unchanged: `rental-housing-monitor/tests/fixtures/gh_purchase_list.html`
- Reuse unchanged: `rental-housing-monitor/tests/fixtures/gh_detail.html`

**Interfaces:**
- Consumes: existing `LHCollector`, `SHCollector`, `GHCollector`, `Announcement`, filters, `MonitorSpec(adapter_ref="rental_housing")`.
- Produces: `RentalHousingAdapter.fetch(monitor_id, spec) -> ObservationBatch` with institution status/warnings and stable item IDs.

- [ ] **Step 1: Write failing compatibility tests against existing fixtures**

```python
async def test_rental_adapter_preserves_existing_announcement_identity(adapter) -> None:
    batch = await adapter.fetch("rental-housing-seoul-gyeonggi", rental_spec())
    assert {item.item_id for item in batch.items} == {
        f"announcement:{canonical_key(item)}" for item in adapter.source_announcements
    }
    assert batch.source_status == {"LH": "ok", "SH": "ok", "GH": "ok"}


async def test_one_institution_failure_preserves_healthy_items(adapter) -> None:
    adapter.sh_result = ParserStructureError(Agency.SH, "목록 파싱", "공고 행 없음")
    batch = await adapter.fetch("rental-housing-seoul-gyeonggi", rental_spec())
    assert batch.source_status == {"LH": "ok", "SH": "failed", "GH": "ok"}
    assert batch.warnings[0].source == "SH"
```

- [ ] **Step 2: Run focused tests and verify the missing adapter**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/adapters/test_rental_housing.py -q`

Expected: FAIL importing `RentalHousingAdapter`.

- [ ] **Step 3: Implement the wrapper without editing collector code**

Instantiate the same HTTP clients, data.go.kr key, GH TLS context, and collector classes as the existing CLI. Execute each collector with `asyncio.to_thread()`, deduplicate by `canonical_key()`, and translate announcements to fields containing `source_id`, `title`, `agency`, `region`, `housing_type`, `target`, `announcement_date`, `application_start_date`, `application_end_date`, and `url`. Use item ID `f"announcement:{canonical_key(announcement)}"`.

Translate `CollectorError` into `SourceWarning(source=agency.value, stage=error.stage, detail=error.detail)` and continue. An unexpected exception becomes a warning whose detail is only its class name. Set source status for all three institutions.

- [ ] **Step 4: Register only the explicit adapter key**

`DefaultAdapterRegistry.resolve(kind, adapter_ref)` returns this adapter only for `(SourceAdapterKind.PYTHON_PLUGIN, "rental_housing")`; unknown or blank plugin references raise a policy error. It never imports a dotted path from configuration.

- [ ] **Step 5: Run existing and compatibility suites, then commit**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/test_lh_collector.py tests/test_sh_collector.py tests/test_gh_collector.py tests/test_runner.py tests/personal_monitor/adapters/test_rental_housing.py -q`

Expected: every old and new rental compatibility test passes.

```bash
git add rental-housing-monitor/src/personal_monitor/adapters rental-housing-monitor/tests/personal_monitor/adapters/test_rental_housing.py
git commit -m "feat: wrap rental housing collectors"
```

### Task 2: Import the data-branch SQLite state idempotently

**Files:**
- Create: `rental-housing-monitor/src/personal_monitor/migration/__init__.py`
- Create: `rental-housing-monitor/src/personal_monitor/migration/import_rental.py`
- Modify: `rental-housing-monitor/src/personal_monitor/cli.py`
- Create: `rental-housing-monitor/tests/personal_monitor/migration/test_import_rental.py`

**Interfaces:**
- Consumes: existing `announcements`, `deliveries`, and `runs` tables; owner ID; target ID; new DB.
- Produces: `import_rental_state(old_path, new_path, owner_id, target_id) -> ImportReport` and CLI `migration import-rental`.

- [ ] **Step 1: Write failing import/idempotency tests**

```python
def test_import_maps_seen_and_delivered_state(old_db, new_db) -> None:
    report = import_rental_state(old_db, new_db, "telegram-user:7", "telegram-main")
    assert report.announcements_imported == 3
    assert report.deliveries_imported == 2
    assert load_item_ids(new_db) == {"announcement:LH:1", "announcement:SH:2", "announcement:GH:3"}


def test_import_is_idempotent(old_db, new_db) -> None:
    first = import_rental_state(old_db, new_db, "telegram-user:7", "telegram-main")
    second = import_rental_state(old_db, new_db, "telegram-user:7", "telegram-main")
    assert second == first.with_no_new_rows()
```

- [ ] **Step 2: Create the fixed rental `MonitorSpec`**

Use `name="서울·경기 임대주택"`, a stable official LH list URL as target metadata, `source_adapter="python_plugin"`, `adapter_ref="rental_housing"`, `fetch_strategy="http"`, schedule `13 12 * * *`, timezone `Asia/Seoul`, `notify_on_no_change=true`, a `new_item` rule, and declared announcement fields. Create user, target, monitor, and approved version only if absent.

- [ ] **Step 3: Map old rows transactionally**

For each old announcement, use `f"announcement:{announcement_key}"`, preserve first/last seen timestamps and canonical JSON fields, and compute the new content hash. For each successful old delivery, create a delivered outbox row with dedupe key `f"{monitor_id}:{item_id}:new_item"`, the new target ID, old Telegram message ID, and delivered timestamp. Use one `BEGIN IMMEDIATE`; any invalid old row aborts the complete import and reports only table/key, never content or secrets.

- [ ] **Step 4: Add and test the CLI command**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m personal_monitor migration import-rental --source data/announcements.db --database /tmp/personal-monitor-import.db --owner telegram-user:7 --target telegram-main --dry-run`

Expected: JSON report with source counts, would-import counts, and no target DB mutation.

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/migration/test_import_rental.py -q`

Expected: mapping, rollback, dry-run, and second-run idempotency tests pass.

- [ ] **Step 5: Commit importer**

```bash
git add rental-housing-monitor/src/personal_monitor/migration rental-housing-monitor/src/personal_monitor/cli.py rental-housing-monitor/tests/personal_monitor/migration
git commit -m "feat: import rental monitor state"
```

### Task 3: Record and evaluate seven-day shadow parity

**Files:**
- Create: `rental-housing-monitor/src/personal_monitor/migration/shadow.py`
- Modify: `rental-housing-monitor/src/personal_monitor/storage/schema.py`
- Modify: `rental-housing-monitor/src/personal_monitor/cli.py`
- Create: `rental-housing-monitor/tests/personal_monitor/migration/test_shadow.py`

**Interfaces:**
- Consumes: normalized old/new results and Asia/Seoul run date.
- Produces: `ShadowComparator.compare()`, `ShadowRepository.record()`, `cutover_ready()`, `migration shadow-run`, and `migration status`.

- [ ] **Step 1: Write failing streak-reset tests**

```python
def test_seven_consecutive_matches_are_ready(shadow_repo) -> None:
    for offset in range(7):
        shadow_repo.record(match_for(date(2026, 7, 22) + timedelta(days=offset)))
    assert shadow_repo.cutover_ready(as_of=date(2026, 7, 28)) is True


def test_one_mismatch_resets_the_streak(shadow_repo) -> None:
    shadow_repo.record(match_for(date(2026, 7, 22)))
    shadow_repo.record(mismatch_for(date(2026, 7, 23), agency="SH"))
    for offset in range(5):
        shadow_repo.record(match_for(date(2026, 7, 24) + timedelta(days=offset)))
    assert shadow_repo.cutover_ready(as_of=date(2026, 7, 28)) is False
```

- [ ] **Step 2: Add shadow schema and safe comparison data**

```sql
CREATE TABLE rental_shadow_results(
  run_date TEXT PRIMARY KEY, old_hash TEXT NOT NULL, new_hash TEXT NOT NULL,
  matched INTEGER NOT NULL, differences_json TEXT NOT NULL,
  old_status_json TEXT NOT NULL, new_status_json TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
```

Hashes cover sorted institution, canonical item ID, filter outcome, and institution status. `differences_json` contains only institution and missing/extra IDs; no titles, URLs, query strings, or response bodies.

- [ ] **Step 3: Implement shadow execution with delivery disabled**

`migration shadow-run` runs `RentalHousingAdapter`, reads the latest authoritative old result from a temporary copy of `origin/data:rental-housing-monitor/data/announcements.db` after that day's QStash workflow finishes, and compares rows whose `last_seen_at` belongs to the latest old run plus its `agency_status`. The personal runtime must use a `NullDeliverySender` and a dedicated shadow method that cannot enqueue outbox rows. A rerun for the same date replaces that date atomically.

- [ ] **Step 4: Implement readiness status**

`migration status` prints `consecutive_matches`, `last_match_date`, `unresolved_differences`, `state_imported`, `duplicate_probe_passed`, and `cutover_ready`. Add `migration duplicate-probe --database --monitor`; it runs the active rental adapter through `NullDeliverySender`, compares every currently observed item against imported observations/deliveries, records the probe result, and cannot write an outbox row. Readiness requires exactly seven consecutive local dates ending on today or yesterday, no unresolved mismatch, successful import, and successful no-send duplicate probe.

- [ ] **Step 5: Run shadow tests and commit**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/migration/test_shadow.py -q`

Expected: streak, gap, mismatch reset, rerun replacement, no-delivery, and readiness tests pass.

```bash
git add rental-housing-monitor/src/personal_monitor/migration rental-housing-monitor/src/personal_monitor/storage/schema.py rental-housing-monitor/src/personal_monitor/cli.py rental-housing-monitor/tests/personal_monitor/migration
git commit -m "feat: verify rental shadow parity"
```

### Task 4: Build isolated Docker Compose services

**Files:**
- Create: `rental-housing-monitor/Dockerfile`
- Create: `rental-housing-monitor/.dockerignore`
- Create: `rental-housing-monitor/compose.yaml`
- Create: `rental-housing-monitor/deploy/entrypoint.sh`
- Create: `rental-housing-monitor/deploy/squid.conf`
- Create: `rental-housing-monitor/deploy/squid.Dockerfile`
- Create: `rental-housing-monitor/tests/personal_monitor/deploy/test_compose.py`
- Create: `rental-housing-monitor/tests/personal_monitor/deploy/test_dockerfile.py`

**Interfaces:**
- Consumes: application source, `.env`, bind-mounted data roots, Codex auth volume.
- Produces: `monitor`, `codex-worker`, and opt-in `profile-bootstrap` services with no Docker socket or public application port.

- [ ] **Step 1: Write failing static deployment-policy tests**

```python
def test_codex_worker_has_no_application_data_mount(compose) -> None:
    worker_mounts = compose["services"]["codex-worker"]["volumes"]
    assert all("/srv/personal-monitor/db" not in mount for mount in worker_mounts)
    assert all("docker.sock" not in mount for mount in worker_mounts)


def test_no_service_publishes_public_port(compose) -> None:
    for service in compose["services"].values():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:")
```

- [ ] **Step 2: Create a pinned runtime image**

Use `python:3.12-slim-bookworm`, install Node.js 22, `age`, `sqlite3`, `curl`, Xvfb/noVNC runtime packages, install the Python wheel, run `scrapling install --force`, and install `@openai/codex@0.144.1`. Create uid/gid 10001 and run as that user. The entrypoint checks directory modes, applies SQLite migrations, and uses `exec "$@"`; it never prints environment values.

- [ ] **Step 3: Define Compose isolation**

`monitor` mounts `/srv/personal-monitor/db`, `adaptive`, encrypted `vault`, logs, and shared `ai-socket`, plus tmpfs at `/run/personal-monitor-profiles`; it runs `personal-monitor serve`. `codex-worker` mounts only `codex-home` and `ai-socket`, uses a read-only root filesystem, `tmpfs` for `/tmp` and `/work`, drops all capabilities, sets `no-new-privileges`, and runs `personal-monitor ai-worker --socket /run/personal-monitor-ai/worker.sock`. Both containers run as service UID 10001 so only they can traverse the mode-`0700` socket directory and connect to its mode-`0600` socket. Both use restart `unless-stopped`; only the monitor has the Telegram token. Neither mounts the Docker socket.

Add an `egress-proxy` Squid service built from `ubuntu:24.04` plus the distribution `squid` package in `deploy/squid.Dockerfile`. `squid.conf` allows only ports 80/443; denies loopback, private, link-local, multicast, reserved IPv4/IPv6, and metadata host destinations before allowing the monitor network; strips `Accept-Encoding`; and sets `reply_body_max_size 10 MB`. The monitor passes `http://egress-proxy:3128` to every user-target Scrapling fetch. The proxy has no published port; a static test requires the denial ACLs to precede the allow rule.

The `profile-bootstrap` service is under Compose profile `admin`, binds noVNC only to `127.0.0.1:6080`, mounts encrypted vault plus an ephemeral tmpfs profile, and stops after bootstrap. It never runs in the default service set.

- [ ] **Step 4: Render and inspect Compose**

Run: `cd rental-housing-monitor && docker compose --env-file .env.example config --quiet`

Expected: exit 0 with no missing interpolation variable required for syntax validation.

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/deploy/test_compose.py tests/personal_monitor/deploy/test_dockerfile.py -q`

Expected: mount, user, read-only, capability, port, pinned-version, and secret-copy tests pass.

- [ ] **Step 5: Commit container deployment**

```bash
git add rental-housing-monitor/Dockerfile rental-housing-monitor/.dockerignore rental-housing-monitor/compose.yaml rental-housing-monitor/deploy rental-housing-monitor/tests/personal_monitor/deploy
git commit -m "feat: containerize personal monitor services"
```

### Task 5: Create encrypted backup and restore tooling

**Files:**
- Create: `rental-housing-monitor/scripts/backup_personal_monitor.sh`
- Create: `rental-housing-monitor/scripts/restore_personal_monitor.sh`
- Create: `rental-housing-monitor/scripts/verify_backup.sh`
- Create: `rental-housing-monitor/deploy/gcs-lifecycle.json`
- Create: `rental-housing-monitor/tests/personal_monitor/deploy/test_backup_scripts.py`

**Interfaces:**
- Consumes: `/srv/personal-monitor`, service-UID-owned `/etc/personal-monitor/master.key`, `AGE_RECIPIENT`, GCS bucket.
- Produces: encrypted daily/weekly archives, checksums, local pre-encryption restore smoke test, and explicit empty-directory restore.

- [ ] **Step 1: Write failing script-policy tests**

```python
def test_backup_uses_sqlite_backup_before_tar(script_text) -> None:
    assert 'sqlite3 "$DATABASE" ".backup' in script_text
    assert script_text.index(".backup") < script_text.index("tar ")


def test_restore_refuses_nonempty_target(restore_text) -> None:
    assert "TARGET_MUST_BE_EMPTY" in restore_text
    assert "age --decrypt" in restore_text
```

- [ ] **Step 2: Implement consistent backup staging**

`backup_personal_monitor.sh` uses `flock`, `mktemp -d`, SQLite `.backup`, copies adaptive data and encrypted vault records, includes the master key and non-secret config inside the staging directory, records SHA-256 checksums, calls `verify_backup.sh` against a second temporary directory, then runs `tar --sort=name --mtime=@0 ... | age --recipient "$AGE_RECIPIENT"`. Upload to `daily/YYYY-MM-DDTHHMMSSZ.tar.age`; on Sunday also copy the same encrypted object to `weekly/YYYY-MM-DDTHHMMSSZ.tar.age`. After a verified upload, list objects by prefix and delete all but the newest seven daily and newest four weekly objects; the lifecycle rules are a delayed safety net. Remove staging with a trapped cleanup.

- [ ] **Step 3: Implement explicit restore**

`restore_personal_monitor.sh` requires an empty target directory, an archive path, and an operator-supplied age identity file. It verifies the archive checksum manifest, restores files with mode restrictions, runs `PRAGMA integrity_check`, and prints only record counts and archive timestamp. It never starts Compose; the operator reviews the result first.

- [ ] **Step 4: Define lifecycle and test scripts**

```json
{
  "rule": [
    {"action": {"type": "Delete"}, "condition": {"age": 8, "matchesPrefix": ["daily/"]}},
    {"action": {"type": "Delete"}, "condition": {"age": 29, "matchesPrefix": ["weekly/"]}}
  ]
}
```

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/deploy/test_backup_scripts.py -q && shellcheck scripts/backup_personal_monitor.sh scripts/restore_personal_monitor.sh scripts/verify_backup.sh`

Expected: policy tests and ShellCheck pass.

- [ ] **Step 5: Commit backup tooling**

```bash
git add rental-housing-monitor/scripts rental-housing-monitor/deploy/gcs-lifecycle.json rental-housing-monitor/tests/personal_monitor/deploy/test_backup_scripts.py
git commit -m "feat: back up personal monitor state"
```

### Task 6: Add read-only GCP preflight and idempotent provisioning

**Files:**
- Create: `rental-housing-monitor/infra/gcp/preflight.sh`
- Create: `rental-housing-monitor/infra/gcp/provision.sh`
- Create: `rental-housing-monitor/infra/gcp/startup.sh`
- Create: `rental-housing-monitor/infra/gcp/README.md`
- Create: `rental-housing-monitor/tests/personal_monitor/deploy/test_gcp_scripts.py`

**Interfaces:**
- Consumes: authenticated gcloud account, project Owner access, billing-enabled project.
- Produces: preflight JSON, Compute API enablement, service account, GCS bucket/lifecycle, firewall rules, and one VM without changing Cloud Run.

- [ ] **Step 1: Write failing mutation-boundary tests**

```python
def test_preflight_contains_no_mutating_gcloud_verbs(preflight_text) -> None:
    for verb in (" create ", " delete ", " update ", " enable ", " add-iam-policy-binding "):
        assert verb not in preflight_text


def test_provision_never_mentions_cloud_run(provision_text) -> None:
    assert "gcloud run" not in provision_text
    assert "local-social-api" not in provision_text
```

- [ ] **Step 2: Implement read-only preflight**

Use constants project `local-social-native-wlk-0720`, region `asia-northeast3`, zones `asia-northeast3-a` then `asia-northeast3-b`, machine `e2-medium`, disk 50 GB. Check active account, billing enabled, billing-budget list permission, Compute API state, region quotas, E2 machine type in each zone, absence of VM name `personal-monitor-1`, and existing Cloud Run service names for a no-change baseline. If Compute API is disabled, report `compute_api_enabled=false` without enabling it. Exit 0 only when billing/account are valid and no conflicting VM exists; quota/repository checks that require the disabled API report `unknown_until_enabled`.

- [ ] **Step 3: Implement idempotent provisioning**

After an execution checkpoint, enable `compute.googleapis.com`, `iap.googleapis.com`, `storage.googleapis.com`, and `billingbudgets.googleapis.com`; rerun quota checks; select the first zone where `e2-medium` exists and quota permits two CPUs. Create service account `personal-monitor-vm`, bucket `gs://local-social-native-wlk-0720-personal-monitor-backups` in `asia-northeast3`, apply `deploy/gcs-lifecycle.json`, and grant only `roles/storage.objectUser` on that bucket.

Look up the project's billing account, verify its currency is KRW, and create one monthly project-filtered budget named `personal-monitor-monthly-50000` with amount `50000KRW` and current-spend thresholds 0.5, 0.8, and 1.0. If the active account lacks billing-budget permission or uses a different currency, provisioning leaves compute resources untouched until the operator grants permission or approves an equivalent amount in that currency.

Create firewall allow rule priority 900 for tag `personal-monitor-iap`, source `35.235.240.0/20`, TCP 22; create deny rule priority 1000 for the same tag, source `0.0.0.0/0`, all protocols. Create VM `personal-monitor-1` with `e2-medium`, Ubuntu 24.04 LTS amd64, 50 GB `pd-balanced`, OS Login metadata, the service account, tag, and startup script. The VM may have an ephemeral external IP for outbound package/image access, but the deny rule blocks all non-IAP ingress.

- [ ] **Step 4: Make startup preparation non-secret**

`startup.sh` installs Docker Engine/Compose plugin, creates `/srv/personal-monitor/{app,db,adaptive,vault,logs,backups}` with owner uid 10001 and mode `0o700`, creates `/etc/personal-monitor` mode `0o700`, and enables systemd. It does not clone private credentials, perform Codex login, write `.env`, or start the application.

- [ ] **Step 5: Run static tests and a read-only live preflight**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/deploy/test_gcp_scripts.py -q && shellcheck infra/gcp/*.sh`

Expected: tests and ShellCheck pass.

Run: `cd rental-housing-monitor && bash infra/gcp/preflight.sh`

Expected before provisioning: JSON names the active account/project, billing true, Compute API state, no conflicting VM when knowable, and makes no change.

- [ ] **Step 6: Commit infrastructure code**

```bash
git add rental-housing-monitor/infra/gcp rental-housing-monitor/tests/personal_monitor/deploy/test_gcp_scripts.py
git commit -m "feat: provision personal monitor VM"
```

### Task 7: Add host operations, health, and migration runbooks

**Files:**
- Create: `rental-housing-monitor/deploy/systemd/personal-monitor.service`
- Create: `rental-housing-monitor/deploy/systemd/personal-monitor-backup.service`
- Create: `rental-housing-monitor/deploy/systemd/personal-monitor-backup.timer`
- Create: `rental-housing-monitor/deploy/systemd/personal-monitor-verify.service`
- Create: `rental-housing-monitor/deploy/systemd/personal-monitor-verify.timer`
- Create: `rental-housing-monitor/docs/operations/gcp-deploy.md`
- Create: `rental-housing-monitor/docs/operations/backup-restore.md`
- Create: `rental-housing-monitor/docs/operations/rental-cutover.md`
- Modify: `rental-housing-monitor/README.md`
- Create: `rental-housing-monitor/tests/personal_monitor/deploy/test_runbooks.py`

**Interfaces:**
- Consumes: provisioned VM, Compose files, operator-held secrets.
- Produces: boot service, daily backup, weekly local restore verification, health commands, exact shadow/cutover/rollback procedures.

- [ ] **Step 1: Define systemd behavior**

Main service runs `docker compose up --remove-orphans`, waits for network-online and Docker, restarts on failure, and stops with `docker compose stop --timeout 90`. Backup timer runs daily at 03:10 Asia/Seoul with persistent catch-up. Verify timer runs Sundays at 04:10 and executes local staging restore verification; it does not require the off-VM age private key.

- [ ] **Step 2: Document secret bootstrap and Codex login**

Over IAP SSH, create root-readable `.env`, create `/etc/personal-monitor/master.key` owned by service uid 10001 with mode 600, and configure `AGE_RECIPIENT`; never paste values into Git/Codex chat. Start only `codex-worker`, enter it with `docker compose exec codex-worker codex login --device-auth`, run `codex login status`, then start the default services. The runbook explicitly removes `OPENAI_API_KEY` and `CODEX_API_KEY` from service environment.

- [ ] **Step 3: Document health and restore checks**

Health commands cover Compose status, DB integrity, scheduler heartbeat, next run, disk free, last backup object, Telegram poll age, and Codex auth as separate checks. The full restore drill downloads one GCS object, uses the operator's off-server age identity on an empty temporary VM/directory, verifies checksums/DB, and never points at the live data directory.

- [ ] **Step 4: Document migration and rollback exactly**

The cutover runbook includes data-branch checkout, dry-run/import, seven daily shadow commands, `migration status`, duplicate probe with `NullDeliverySender`, QStash pause, one manual production run, scheduler enable, and rollback by stopping Compose then resuming QStash. It states that the GitHub workflow and `data` branch remain untouched.

- [ ] **Step 5: Test runbook command/file references and commit**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/deploy/test_runbooks.py -q`

Expected: every referenced repository path exists and every `personal-monitor` command parses with `--help`.

```bash
git add rental-housing-monitor/deploy/systemd rental-housing-monitor/docs/operations rental-housing-monitor/README.md rental-housing-monitor/tests/personal_monitor/deploy/test_runbooks.py
git commit -m "docs: add personal monitor operations runbooks"
```

### Task 8: Provision and run seven-day shadow mode

**Files:**
- Modify only when a verified defect is found in Tasks 1-7.
- Record operational evidence in: `/srv/personal-monitor/logs/shadow-status.json` on the VM, not in Git.

**Interfaces:**
- Consumes: tested infrastructure scripts, operator confirmation, live source credentials, old data branch.
- Produces: running VM, encrypted backup, live Codex login, and seven consecutive shadow matches with no Telegram delivery.

- [ ] **Step 1: Re-run preflight and obtain the execution checkpoint**

Run: `cd rental-housing-monitor && bash infra/gcp/preflight.sh`

Expected: billing/account valid, no conflicting VM, and quota either sufficient or explicitly unknown because Compute API is disabled. Review the output before the first mutating command.

- [ ] **Step 2: Provision the separate VM**

Run: `cd rental-housing-monitor && bash infra/gcp/provision.sh`

Expected: script prints selected zone, VM name, bucket name, IAP-only ingress rules, and exits 0. It does not change any Cloud Run service.

- [ ] **Step 3: Deploy, authenticate, and verify backups**

Follow `docs/operations/gcp-deploy.md`, then run Compose policy/health commands and one backup. Download the object metadata and compare the uploaded SHA-256; run the local staging restore smoke test. Do not enable the new rental schedule yet.

- [ ] **Step 4: Run shadow mode for seven consecutive days**

At each 12:13 Asia/Seoul cycle, let QStash/GitHub Actions remain authoritative, export its normalized result, run the new adapter with delivery disabled, and record comparison. After each day run:

Run: `personal-monitor migration status --database /srv/personal-monitor/db/monitor.db`

Expected on day seven: `consecutive_matches=7`, no unresolved differences, import complete, duplicate probe passed, and `cutover_ready=true`. Any mismatch or missing date resets the gate.

- [ ] **Step 5: Preserve evidence without committing sensitive data**

Store only hashes, institution status, missing/extra canonical IDs, timestamps, and readiness in the VM DB/log. Do not commit live result sets, credentials, URLs with queries, or Telegram IDs.

### Task 9: Cut over, observe, and retire only the QStash schedule

**Files:**
- Modify: `rental-housing-monitor/README.md` only to record the completed cutover date and new production location after success.
- Keep unchanged: `.github/workflows/rental-housing-monitor.yml`.
- Keep unchanged: remote `data` branch.

**Interfaces:**
- Consumes: `cutover_ready=true`, verified backup, QStash access, Telegram delivery.
- Produces: VM-authoritative rental execution without duplicate alerts and a seven-day rollback window.

- [ ] **Step 1: Pause QStash without deleting it**

In the QStash console, open Schedules → `rental-housing-monitor-daily` → Pause. Immediately fetch its details and verify `isPaused=true`. Do not delete it during the rollback window.

- [ ] **Step 2: Run the no-send duplicate probe after final import**

Run: `personal-monitor migration duplicate-probe --database /srv/personal-monitor/db/monitor.db --monitor rental-housing-seoul-gyeonggi`

Expected: `pending_existing_announcements=0` and no Telegram call.

- [ ] **Step 3: Run one controlled production execution**

Run: `personal-monitor run-once --database /srv/personal-monitor/db/monitor.db --monitor rental-housing-seoul-gyeonggi --delivery enabled`

Expected: no historical duplicate; either genuinely new notices, the exact healthy no-new message, or an institution-specific partial-failure warning. Then enable the internal schedule.

- [ ] **Step 4: Observe seven successful VM days**

Check run status, institution status, delivery idempotency, heartbeat, disk, and backup daily. On a critical regression, stop Compose and resume the paused QStash schedule; do not run both bot/schedulers simultaneously.

- [ ] **Step 5: Delete QStash only after the rollback window**

Run from a trusted terminal with `QSTASH_TOKEN` already set:

```bash
curl --fail-with-body --request DELETE \
  --url https://qstash.upstash.io/v2/schedules/rental-housing-monitor-daily \
  --header "Authorization: Bearer ${QSTASH_TOKEN}"
```

Expected: HTTP 200. Then list schedules and verify the ID is absent. Keep the GitHub workflow and data branch as dormant rollback artifacts until a separate cleanup approval.

- [ ] **Step 6: Record the cutover and run final verification**

Update README with the date, VM scheduler ownership, backup location name, and rollback status without any secret. Run:

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest -q && ../.venv/bin/python -m ruff check . && ../.venv/bin/python -m compileall -q src && cd .. && git diff --check`

Expected: all commands pass; workflow remains dispatch-only and unchanged.

```bash
git add rental-housing-monitor/README.md
git commit -m "docs: record rental monitor cutover"
```

## Deployment plan completion gate

Completion requires all of the following evidence: seven-day shadow parity, import report with no duplicate pending delivery, QStash removed only after the seven-day VM observation window, seven successful VM runs, one downloaded GCS archive restored into an empty location with `PRAGMA integrity_check=ok`, no public non-IAP ingress, Codex ChatGPT authentication, and unchanged `local-social-api` Cloud Run revision/configuration.
