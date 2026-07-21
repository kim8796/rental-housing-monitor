# Off-Peak Monitor Schedule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the daily rental-housing monitor from 12:00 to 12:13 KST, deploy it, and run today's missed check once.

**Architecture:** Keep the existing GitHub Actions workflow and application unchanged except for the cron minute. Pin the schedule in a workflow regression test and keep the operator documentation synchronized.

**Tech Stack:** GitHub Actions YAML, pytest, Markdown, GitHub CLI

## Global Constraints

- Use only the existing GitHub Actions scheduler; do not add an external scheduler or backup cron.
- Preserve the existing monitor, SQLite, Telegram, retry, and logging behavior.
- Run the missed 2026-07-21 check once through `workflow_dispatch` after deployment.

---

### Task 1: Schedule the monitor at 12:13 KST

**Files:**
- Modify: `rental-housing-monitor/tests/test_workflow.py`
- Modify: `.github/workflows/rental-housing-monitor.yml`
- Modify: `rental-housing-monitor/README.md`

**Interfaces:**
- Consumes: GitHub Actions UTC cron syntax.
- Produces: A daily `13 3 * * *` schedule, equivalent to 12:13 KST.

- [ ] **Step 1: Write the failing regression test**

Rename the schedule test to `test_workflow_runs_at_kst_1213_and_prevents_overlap` and require:

```python
assert "cron: '13 3 * * *'" in text
```

- [ ] **Step 2: Verify the regression test fails**

Run: `python -m pytest tests/test_workflow.py::test_workflow_runs_at_kst_1213_and_prevents_overlap -q`

Expected: one assertion failure because the workflow still contains `0 3 * * *`.

- [ ] **Step 3: Apply the minimal configuration and documentation change**

Set the workflow schedule to:

```yaml
- cron: '13 3 * * *'
```

Document it as:

```markdown
- cron `13 3 * * *`: UTC 03:13, 한국시간 매일 12:13
```

- [ ] **Step 4: Verify the focused and full test suites**

Run: `python -m pytest tests/test_workflow.py -q`

Expected: all workflow tests pass.

Run: `python -m pytest -q`

Expected: all project tests pass with zero failures.

- [ ] **Step 5: Commit and deploy**

```bash
git add .github/workflows/rental-housing-monitor.yml rental-housing-monitor/tests/test_workflow.py rental-housing-monitor/README.md docs/superpowers/plans/2026-07-21-avoid-top-of-hour-schedule.md
git commit -m "fix: avoid top-of-hour scheduler load"
git push origin main
```

- [ ] **Step 6: Run and verify today's missed check**

```bash
gh workflow run rental-housing-monitor.yml --repo kim8796/rental-housing-monitor --ref main
```

Watch the resulting run to completion and confirm the `Run monitor` step completed successfully. If it fails, report the failing step and log excerpt instead of claiming delivery.
