# Personal Monitor Platform Implementation Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this roadmap plan-by-plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evolve the current rental-housing monitor into a portable personal monitoring platform without interrupting the daily production run.

**Architecture:** Keep `rental_monitor` unchanged while a new `personal_monitor` package is built and verified in four gated phases. The new package separates deterministic execution from AI-assisted control, then imports the existing rental monitor only after shadow parity is proven.

**Tech Stack:** Python 3.12+, SQLite WAL, Pydantic 2, croniter, Scrapling 0.4.11, httpx, Codex CLI with ChatGPT authentication, Telegram Bot API, Docker Compose, Google Compute Engine, GCS, pytest, Ruff.

## Global Constraints

- Keep `.github/workflows/rental-housing-monitor.yml` and QStash schedule `rental-housing-monitor-daily` active until the final cutover gate.
- Preserve the existing daily rental-housing execution time at 12:13 Asia/Seoul.
- Regular monitor runs must never call Codex; Codex is only for intent parsing, new `MonitorSpec` proposals, and repair proposals.
- Use GPT-5.6 Terra with `medium` effort for two attempts, then GPT-5.6 Sol with `high` effort for one final attempt.
- Force ChatGPT authentication for Codex and fail closed when `OPENAI_API_KEY` or `CODEX_API_KEY` is present.
- Require explicit Telegram confirmation for create, update, schedule change, repair activation, and delete operations.
- Permit only `http` and `https` targets and reject loopback, private, link-local, multicast, metadata, unsafe redirect, and DNS-rebinding destinations.
- Default to a six-hour schedule, enforce a 15-minute minimum, and use at most two minutes of deterministic jitter.
- Limit HTTP fetches to 30 seconds and browser fetches to 90 seconds, five redirects, and 10 MiB after decompression.
- Keep HTTP concurrency at four, browser concurrency at one, and the default per-host interval at ten seconds.
- Store no plaintext secret in Git, logs, SQLite, Docker images, Telegram, or GCS backups.
- Keep existing `local-social-api` Cloud Run resources unchanged.

---

## Plan set and dependency order

| Order | Plan | Independently verifiable result | Entry gate |
|---|---|---|---|
| 1 | [Core runtime](2026-07-22-personal-monitor-core.md) | Versioned `MonitorSpec`, SQLite registry, scheduler leases, rule evaluation, and idempotent outbox work against fake adapters | Existing 52-test baseline green |
| 2 | [Scrapling and security](2026-07-22-personal-monitor-scrapling-security.md) | A manually supplied `MonitorSpec` safely monitors static, dynamic, and session-backed fixtures without AI | Core runtime complete |
| 3 | [Telegram and Codex control plane](2026-07-22-personal-monitor-telegram-codex.md) | The allowed Telegram user can create and manage a monitor in Korean natural language with preview and confirmation | Scrapling plan complete |
| 4 | [Rental migration and GCP deployment](2026-07-22-personal-monitor-migration-deployment.md) | Docker Compose runs on the new GCE VM, seven-day shadow parity passes, state imports, and QStash is cut over without duplicate delivery | Telegram/Codex plan complete |

The plans are intentionally sequential. A later plan may consume public interfaces from earlier plans, but it must not rewrite those interfaces without updating the earlier plan and its contract tests first.

## Cross-plan review gates

- [ ] **Gate 1: Preserve the existing baseline**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest -q && ../.venv/bin/python -m ruff check . && ../.venv/bin/python -m compileall -q src`

Expected: `52 passed`, `All checks passed!`, and exit code 0.

- [ ] **Gate 2: Finish the core runtime before installing browser dependencies**

Run: `git log -1 --format=%s`

Expected: the final core-plan commit is `feat: add personal monitor runtime CLI`.

- [ ] **Gate 3: Prove deterministic monitoring before enabling Codex**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/scraping tests/personal_monitor/security -q`

Expected: all static, dynamic, login-profile, robots, SSRF, extraction, and recovery contract tests pass with no Codex process started.

- [ ] **Gate 4: Prove AI is absent from scheduled execution**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest tests/personal_monitor/test_ai_boundary.py -q`

Expected: scheduled execution passes while the fake Codex worker raises on every invocation, and onboarding invokes the expected Terra/Terra/Sol sequence only when validation fails.

- [ ] **Gate 5: Preserve production until migration evidence exists**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m personal_monitor migration status --database /srv/personal-monitor/db/monitor.db`

Expected before cutover: seven consecutive `matched` Asia/Seoul dates, no unresolved mismatch, successful import, and `cutover_ready=true`.

- [ ] **Gate 6: Final repository verification**

Run: `cd rental-housing-monitor && ../.venv/bin/python -m pytest -q && ../.venv/bin/python -m ruff check . && ../.venv/bin/python -m compileall -q src && cd .. && git diff --check`

Expected: all commands exit 0 and no unrelated files are changed.

## Rollback boundaries

- Before GCP deployment, rollback is a Git revert of only the new `personal_monitor` commits; `rental_monitor` remains the production path.
- During shadow mode, stop only the new Compose services. QStash and GitHub Actions remain authoritative.
- During the first seven days after cutover, keep the QStash schedule paused rather than deleted so it can be resumed if the VM fails.
- Delete the QStash schedule only after seven successful production VM runs and a verified restore test.

## Current evidence

- Baseline commit before the design work: `472154a`.
- Approved design commit: `8077b27`.
- Baseline verification on 2026-07-22: 52 pytest tests passed; Ruff and compileall passed using `../.venv/bin/python`.
- Scrapling current release checked on 2026-07-22: `0.4.11`; install `scrapling[fetchers]>=0.4.11,<0.5` and refresh its browser assets during image build.
- Codex official automation interface: `codex exec --output-schema`; headless ChatGPT login uses `codex login --device-auth`, and `codex login status` verifies cached authentication.
