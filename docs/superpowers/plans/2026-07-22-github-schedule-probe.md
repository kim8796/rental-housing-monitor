# GitHub Schedule Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Determine whether GitHub emits scheduled events for this repository without touching Telegram, SQLite, secrets, or the production monitor.

**Architecture:** Add a separate minimal workflow scheduled every five minutes. Protect its isolation with a repository test, deploy it to `main`, and query GitHub's Actions API for up to twenty minutes for a run whose event is `schedule`.

**Tech Stack:** GitHub Actions YAML, pytest, GitHub CLI

## Global Constraints

- The probe must not check out code, read secrets, call Telegram, run Python, or read/write SQLite.
- The probe must have no repository write permission.
- Do not modify the production rental housing workflow during the diagnostic phase.
- Preserve unrelated untracked user files.

---

### Task 1: Add an isolated schedule probe

**Files:**
- Create: `.github/workflows/schedule-probe.yml`
- Modify: `rental-housing-monitor/tests/test_workflow.py`

**Interfaces:**
- Consumes: GitHub's `schedule` and `workflow_dispatch` event emitters.
- Produces: A `Schedule Probe` workflow run containing only the trigger name and UTC timestamp.

- [ ] **Step 1: Write the failing regression test**

Add a `PROBE_WORKFLOW` path and this test:

```python
def test_schedule_probe_is_frequent_and_isolated() -> None:
    text = PROBE_WORKFLOW.read_text(encoding="utf-8")

    assert "cron: '*/5 * * * *'" in text
    assert "permissions: {}" in text
    assert "timeout-minutes: 2" in text
    for forbidden in ("actions/checkout", "secrets.", "TELEGRAM", "SQLite", "rental_monitor"):
        assert forbidden not in text
```

- [ ] **Step 2: Verify the test fails because the probe is absent**

Run:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_schedule_probe_is_frequent_and_isolated -q
```

Expected: failure with `FileNotFoundError` for `.github/workflows/schedule-probe.yml`.

- [ ] **Step 3: Add the minimal probe workflow**

Create `.github/workflows/schedule-probe.yml` with:

```yaml
name: Schedule Probe

on:
  schedule:
    - cron: '*/5 * * * *'
  workflow_dispatch:

permissions: {}

jobs:
  probe:
    runs-on: ubuntu-latest
    timeout-minutes: 2
    steps:
      - name: Report trigger
        run: |
          echo "event=${{ github.event_name }}"
          date -u
```

- [ ] **Step 4: Verify focused and full test suites**

Run:

```bash
.venv/bin/python -m pytest tests/test_workflow.py -q
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
git diff --check
```

Expected: all commands exit zero; the full suite reports 53 passing tests.

- [ ] **Step 5: Commit and push the probe to main**

```bash
git add .github/workflows/schedule-probe.yml rental-housing-monitor/tests/test_workflow.py docs/superpowers/plans/2026-07-22-github-schedule-probe.md
git commit -m "test: probe GitHub schedule delivery"
git push origin main
```

- [ ] **Step 6: Observe scheduled delivery for twenty minutes**

Poll the API every few minutes:

```bash
gh run list --repo kim8796/rental-housing-monitor --workflow schedule-probe.yml --event schedule --limit 5 --json databaseId,event,status,conclusion,createdAt,url
```

Success: at least one returned run has `event: schedule`.

Failure: the API still returns an empty list after twenty minutes and at least three eligible five-minute boundaries have passed.
