# Data Branch Snapshot Compaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep one year of daily rental-monitor operation bounded by retaining only the latest SQLite snapshot commit while preserving all announcement and delivery deduplication records.

**Architecture:** Add repository-level retention and SQLite compaction, then replace the accumulating `data`-branch worktree flow with a focused shell script that builds a new parentless Git commit containing only the current database. The workflow uses `force-with-lease` so an unexpected concurrent update is rejected instead of overwritten.

**Tech Stack:** Python 3.12, SQLite, Bash, Git plumbing commands, GitHub Actions, pytest, Ruff

## Global Constraints

- Preserve every `announcements` and `deliveries` row; only `runs` rows older than 90 days may be deleted.
- Store only the anonymized delivery key `telegram-default`; never persist the raw Telegram chat ID in SQLite.
- Keep the existing daily schedule at UTC `03:00` (KST `12:00`) and `workflow_dispatch` support.
- Keep `concurrency` with `cancel-in-progress: false` and `contents: write` permission.
- Persist successful delivery state even when a later Telegram send or institution collection fails.
- Keep Actions log artifacts for 14 days.
- Do not modify or stage unrelated `uyi-pet-*` paths.

---

## File Structure

- Modify `rental-housing-monitor/src/rental_monitor/repository.py`: prune old run rows, run `VACUUM`, and optionally compact before close.
- Modify `rental-housing-monitor/src/rental_monitor/__main__.py`: request repository compaction on every CLI shutdown path.
- Modify `rental-housing-monitor/tests/test_repository.py`: verify retention, deduplication-state preservation, and post-`VACUUM` integrity.
- Create `rental-housing-monitor/scripts/persist_data_snapshot.sh`: create and lease-push a single parentless database snapshot commit.
- Create `rental-housing-monitor/tests/test_snapshot_script.py`: exercise the script against a temporary bare Git remote twice.
- Modify `.github/workflows/rental-housing-monitor.yml`: call the snapshot script instead of appending to the previous `data` history.
- Modify `rental-housing-monitor/tests/test_workflow.py`: enforce the single-snapshot workflow contract.
- Modify `rental-housing-monitor/README.md`: document 90-day run retention and single-commit state storage.

---

### Task 1: Compact SQLite State Without Losing Deduplication Records

**Files:**
- Modify: `rental-housing-monitor/tests/test_repository.py`
- Modify: `rental-housing-monitor/src/rental_monitor/repository.py:3-7,185-186`
- Modify: `rental-housing-monitor/src/rental_monitor/__main__.py:60-65`

**Interfaces:**
- Consumes: existing `AnnouncementRepository`, `start_run()`, `upsert_seen()`, and `mark_delivered()` APIs.
- Produces: `AnnouncementRepository.compact(*, retention_days: int = 90, now: datetime | None = None) -> None` and `AnnouncementRepository.close(*, compact: bool = False, retention_days: int = 90, now: datetime | None = None) -> None`.

- [ ] **Step 1: Write failing repository retention tests**

Replace the existing datetime import and add SQLite, then append the tests:

```python
import sqlite3
from datetime import UTC, date, datetime, timedelta


def test_compact_removes_only_runs_older_than_retention(tmp_path) -> None:
    database_path = tmp_path / "announcements.db"
    repository = AnnouncementRepository(database_path)
    now = datetime(2026, 7, 20, 3, tzinfo=UTC)
    old_run = repository.start_run(now - timedelta(days=91))
    recent_run = repository.start_run(now - timedelta(days=89))
    item = notice()
    repository.upsert_seen([item], observed_at=now - timedelta(days=120))
    repository.mark_delivered(item, "telegram-default", 123, delivered_at=now)

    repository.compact(now=now)

    run_ids = [row[0] for row in repository.connection.execute("SELECT id FROM runs")]
    assert run_ids == [recent_run]
    assert old_run not in run_ids
    assert repository.connection.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 1
    assert repository.connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 1
    assert repository.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_close_can_compact_and_leave_reopenable_database(tmp_path) -> None:
    database_path = tmp_path / "announcements.db"
    repository = AnnouncementRepository(database_path)
    now = datetime(2026, 7, 20, 3, tzinfo=UTC)
    repository.start_run(now - timedelta(days=120))

    repository.close(compact=True, now=now)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
cd rental-housing-monitor
python -m pytest -q tests/test_repository.py::test_compact_removes_only_runs_older_than_retention tests/test_repository.py::test_close_can_compact_and_leave_reopenable_database
```

Expected: both tests fail because `AnnouncementRepository` has no `compact()` method and `close()` does not accept `compact` or `now`.

- [ ] **Step 3: Implement retention and compaction**

Change the datetime import in `repository.py` to:

```python
from datetime import UTC, datetime, timedelta
```

Replace `close()` with the following methods:

```python
    def compact(
        self,
        *,
        retention_days: int = 90,
        now: datetime | None = None,
    ) -> None:
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
        self.connection.execute(
            "DELETE FROM runs WHERE started_at < ?",
            (cutoff.isoformat(),),
        )
        self.connection.commit()
        self.connection.execute("VACUUM")

    def close(
        self,
        *,
        compact: bool = False,
        retention_days: int = 90,
        now: datetime | None = None,
    ) -> None:
        try:
            if compact:
                self.compact(retention_days=retention_days, now=now)
        finally:
            self.connection.close()
```

In `__main__.py`, change the finalizer so successful compaction creates a workspace-only readiness marker:

```python
    finally:
        repository.close(compact=True)
        settings.database_path.with_suffix(settings.database_path.suffix + ".ready").touch()
```

Because `close(compact=True)` raises before `touch()` when compaction fails, the marker proves that the DB is safe to snapshot. The marker is never added to Git.

- [ ] **Step 4: Run repository and full tests**

Run:

```bash
cd rental-housing-monitor
python -m pytest -q tests/test_repository.py
python -m pytest -q
python -m ruff check .
```

Expected: repository tests pass, then all 51 tests pass, and Ruff prints `All checks passed!`.

- [ ] **Step 5: Commit Task 1**

```bash
git add rental-housing-monitor/src/rental_monitor/repository.py rental-housing-monitor/src/rental_monitor/__main__.py rental-housing-monitor/tests/test_repository.py
git commit -m "feat: compact retained monitor state"
```

---

### Task 2: Replace Accumulating Git History With One Snapshot Commit

**Files:**
- Create: `rental-housing-monitor/scripts/persist_data_snapshot.sh`
- Create: `rental-housing-monitor/tests/test_snapshot_script.py`

**Interfaces:**
- Consumes: positional arguments `DATABASE_PATH`, optional `REMOTE` (default `origin`), and optional `BRANCH` (default `data`).
- Produces: one parentless commit at `refs/heads/<BRANCH>` containing only `rental-housing-monitor/data/announcements.db`; exits nonzero on a lease conflict.

- [ ] **Step 1: Write the failing snapshot integration test**

Create `rental-housing-monitor/tests/test_snapshot_script.py`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts/persist_data_snapshot.sh"


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_repeated_snapshot_replaces_history_and_database(tmp_path) -> None:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    database = source / "data/announcements.db"
    git("init", "--bare", str(remote), cwd=tmp_path)
    source.mkdir()
    git("init", cwd=source)
    git("config", "user.name", "Snapshot Test", cwd=source)
    git("config", "user.email", "snapshot@example.com", cwd=source)
    git("remote", "add", "origin", str(remote), cwd=source)
    database.parent.mkdir(parents=True)

    database.write_bytes(b"first snapshot")
    subprocess.run([SCRIPT, database, "origin", "data"], cwd=source, check=True)
    first_head = git("ls-remote", "origin", "refs/heads/data", cwd=source).split()[0]

    database.write_bytes(b"second snapshot")
    subprocess.run([SCRIPT, database, "origin", "data"], cwd=source, check=True)
    second_head = git("ls-remote", "origin", "refs/heads/data", cwd=source).split()[0]
    git("fetch", "origin", "+refs/heads/data:refs/remotes/origin/data", cwd=source)

    assert first_head != second_head
    assert git("rev-list", "--count", "origin/data", cwd=source) == "1"
    assert git("ls-tree", "-r", "--name-only", "origin/data", cwd=source) == (
        "rental-housing-monitor/data/announcements.db"
    )
    stored = subprocess.run(
        ["git", "show", "origin/data:rental-housing-monitor/data/announcements.db"],
        cwd=source,
        check=True,
        capture_output=True,
    ).stdout
    assert stored == b"second snapshot"
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
cd rental-housing-monitor
python -m pytest -q tests/test_snapshot_script.py
```

Expected: FAIL because `scripts/persist_data_snapshot.sh` does not exist.

- [ ] **Step 3: Implement the parentless snapshot script**

Create `rental-housing-monitor/scripts/persist_data_snapshot.sh` with executable mode:

```bash
#!/usr/bin/env bash
set -euo pipefail

database_path=${1:?usage: persist_data_snapshot.sh DATABASE_PATH [REMOTE] [BRANCH]}
remote=${2:-origin}
branch=${3:-data}
remote_ref="refs/heads/$branch"

if [[ ! -f "$database_path" ]]; then
  printf 'database does not exist: %s\n' "$database_path" >&2
  exit 1
fi

old_sha=$(git ls-remote --heads "$remote" "$remote_ref" | awk '{print $1}')
database_blob=$(git hash-object -w "$database_path")
data_tree=$(printf '100644 blob %s\tannouncements.db\n' "$database_blob" | git mktree)
project_tree=$(printf '040000 tree %s\tdata\n' "$data_tree" | git mktree)
root_tree=$(printf '040000 tree %s\trental-housing-monitor\n' "$project_tree" | git mktree)
snapshot_commit=$(printf 'chore(data): update rental monitor snapshot\n' | git commit-tree "$root_tree")

if [[ -n "$old_sha" ]]; then
  git push \
    --force-with-lease="$remote_ref:$old_sha" \
    "$remote" \
    "$snapshot_commit:$remote_ref"
else
  git push "$remote" "$snapshot_commit:$remote_ref"
fi
```

Then run:

```bash
chmod +x rental-housing-monitor/scripts/persist_data_snapshot.sh
```

- [ ] **Step 4: Run the integration test twice and verify GREEN**

Run:

```bash
cd rental-housing-monitor
python -m pytest -q tests/test_snapshot_script.py
python -m pytest -q tests/test_snapshot_script.py
python -m ruff check .
```

Expected: the test passes both times and Ruff prints `All checks passed!`.

- [ ] **Step 5: Commit Task 2**

```bash
git add rental-housing-monitor/scripts/persist_data_snapshot.sh rental-housing-monitor/tests/test_snapshot_script.py
git commit -m "feat: persist one database snapshot commit"
```

---

### Task 3: Connect GitHub Actions and Document the Retention Policy

**Files:**
- Modify: `.github/workflows/rental-housing-monitor.yml:52-79`
- Modify: `rental-housing-monitor/tests/test_workflow.py:29-39`
- Modify: `rental-housing-monitor/README.md:89-116`

**Interfaces:**
- Consumes: `scripts/persist_data_snapshot.sh DATABASE_PATH REMOTE BRANCH` from Task 2.
- Produces: daily Actions runs that restore the prior DB and replace `data` with one current snapshot commit.

- [ ] **Step 1: Tighten the workflow contract test before editing YAML**

Replace `test_workflow_persists_only_database_on_data_branch` and extend the required-file test in `tests/test_workflow.py`:

```python
def test_workflow_replaces_data_branch_with_single_snapshot() -> None:
    text = workflow_text()

    assert "data/announcements.db.ready" in text
    assert "scripts/persist_data_snapshot.sh data/announcements.db origin data" in text
    assert "git worktree add" not in text
    assert "git push origin HEAD:refs/heads/data" not in text


def test_required_operator_files_exist() -> None:
    assert (PROJECT / ".env.example").is_file()
    assert (PROJECT / "README.md").is_file()
    assert (PROJECT / "scripts/persist_data_snapshot.sh").is_file()
```

- [ ] **Step 2: Run the workflow test and verify RED**

Run:

```bash
cd rental-housing-monitor
python -m pytest -q tests/test_workflow.py::test_workflow_replaces_data_branch_with_single_snapshot
```

Expected: FAIL because the workflow still contains `git worktree add` and does not invoke the script.

- [ ] **Step 3: Replace the accumulating persistence block**

In `.github/workflows/rental-housing-monitor.yml`, replace the body of `Persist SQLite state on data branch` with:

```yaml
      - name: Persist SQLite state on data branch
        if: always()
        run: |
          if [ ! -f data/announcements.db ]; then
            exit 0
          fi
          if [ ! -f data/announcements.db.ready ]; then
            echo "SQLite compaction did not complete; preserving the existing data branch" >&2
            exit 1
          fi
          git config user.name github-actions[bot]
          git config user.email 41898282+github-actions[bot]@users.noreply.github.com
          scripts/persist_data_snapshot.sh data/announcements.db origin data
```

- [ ] **Step 4: Update README operation details**

Replace the paragraph beginning `첫 실행 때 data 브랜치가 없으면` with:

```markdown
첫 실행 때 `data` 브랜치가 없으면 자동 생성합니다. 이후 매 실행마다 `rental-housing-monitor/data/announcements.db`만 포함하는 새로운 단일 스냅샷 커밋으로 `data` 브랜치를 교체합니다. 과거 DB 커밋은 보존하지 않으므로 장기간 운영해도 접근 가능한 Git 이력이 누적되지 않습니다. `force-with-lease`가 예상하지 못한 동시 갱신을 감지하면 기존 상태를 덮어쓰지 않고 실행을 실패시킵니다.
```

Extend the storage section with:

```markdown
`announcements`와 `deliveries`는 중복 전송 방지를 위해 계속 보존합니다. `runs`는 최근 90일만 유지하며 종료 시 SQLite `VACUUM`으로 사용하지 않는 공간을 회수합니다. `deliveries`에는 실제 Telegram chat ID 대신 비식별 키 `telegram-default`가 저장됩니다.
```

Replace the obsolete bot-replacement bullet with:

```markdown
- Telegram bot 또는 chat을 바꿔도 기본 비식별 delivery 키가 같으면 과거 공고를 다시 보내지 않습니다. 전체 공고를 새로 받고 싶을 때만 `TELEGRAM_DELIVERY_TARGET`을 새로운 값으로 변경합니다.
```

- [ ] **Step 5: Run complete verification**

Run:

```bash
cd rental-housing-monitor
python -m pytest -q
python -m ruff check .
python -m compileall -q src
```

Expected: all 52 tests pass, Ruff prints `All checks passed!`, and compileall exits 0 without output.

- [ ] **Step 6: Commit Task 3**

```bash
git add .github/workflows/rental-housing-monitor.yml rental-housing-monitor/tests/test_workflow.py rental-housing-monitor/README.md
git commit -m "feat: bound data branch snapshot history"
```

---

### Task 4: Deploy and Verify the One-Year Retention Path

**Files:**
- No source file changes expected.
- Verify remote branches `main` and `data` plus the downloaded Actions artifact.

**Interfaces:**
- Consumes: completed Tasks 1-3, GitHub repository `kim8796/rental-housing-monitor`, and its existing Actions secrets.
- Produces: deployed workflow with a one-commit `data` branch and a verified successful monitor run.

- [ ] **Step 1: Recheck scope and push main**

Run:

```bash
git status -sb
git log --oneline -4
git push origin main
```

Expected: only unrelated untracked `uyi-pet-*` paths remain, and `main` pushes successfully.

- [ ] **Step 2: Trigger and watch the workflow**

Run:

```bash
gh workflow run rental-housing-monitor.yml --repo kim8796/rental-housing-monitor --ref main
run_id=$(gh run list --repo kim8796/rental-housing-monitor --workflow rental-housing-monitor.yml --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$run_id" --repo kim8796/rental-housing-monitor --exit-status
```

Expected: checkout, install, restore, monitor, snapshot persistence, and log upload all complete successfully.

- [ ] **Step 3: Verify the remote snapshot and operational log**

Resolve the most recent completed run and inspect it:

```bash
run_id=$(gh run list --repo kim8796/rental-housing-monitor --workflow rental-housing-monitor.yml --limit 1 --json databaseId --jq '.[0].databaseId')
git fetch origin '+refs/heads/data:refs/remotes/origin/data'
git rev-list --count origin/data
git ls-tree -r --name-only origin/data
artifact_dir=$(mktemp -d)
gh run download "$run_id" --repo kim8796/rental-housing-monitor --dir "$artifact_dir"
rg "실행 완료|기관 수집 실패|실행 실패" "$artifact_dir"
```

Expected:

```text
1
rental-housing-monitor/data/announcements.db
```

The monitor log contains `실행 완료 status=success`; no institution or execution failure line is present.

- [ ] **Step 4: Verify retained data and anonymization**

Extract the remote DB to a temporary directory and query it without printing secret values:

```bash
audit_dir=$(mktemp -d)
git show origin/data:rental-housing-monitor/data/announcements.db > "$audit_dir/announcements.db"
DB_PATH="$audit_dir/announcements.db" python3 - <<'PY'
import os
import sqlite3
from datetime import UTC, datetime, timedelta

connection = sqlite3.connect(os.environ["DB_PATH"])
try:
    print("INTEGRITY=" + connection.execute("PRAGMA integrity_check").fetchone()[0])
    print("ANNOUNCEMENTS=" + str(connection.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]))
    print("DELIVERIES=" + str(connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]))
    keys = [row[0] for row in connection.execute("SELECT DISTINCT chat_id FROM deliveries")]
    print("DELIVERY_KEYS=" + ",".join(keys))
    print("RUNS=" + str(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]))
    cutoff = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    old_runs = connection.execute(
        "SELECT COUNT(*) FROM runs WHERE started_at < ?", (cutoff,)
    ).fetchone()[0]
    print("RUNS_OLDER_THAN_90_DAYS=" + str(old_runs))
finally:
    connection.close()
PY
```

Expected: `INTEGRITY=ok`, announcement and delivery counts are nonzero, `DELIVERY_KEYS=telegram-default`, and `RUNS_OLDER_THAN_90_DAYS=0`.

- [ ] **Step 5: Record final evidence**

Report the public repository URL, successful Actions run URL, test count, `data` branch commit count, DB integrity, retained counts, and the unchanged daily KST noon schedule. Do not print the API key, Bot Token, or Telegram chat ID.
