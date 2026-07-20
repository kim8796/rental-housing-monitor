from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/rental-housing-monitor.yml"
PROJECT = Path(__file__).resolve().parents[1]


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_runs_at_kst_noon_and_prevents_overlap() -> None:
    text = workflow_text()

    assert "cron: '0 3 * * *'" in text
    assert "contents: write" in text
    assert "concurrency:" in text
    assert "cancel-in-progress: false" in text


def test_workflow_supplies_secrets_and_always_uploads_log() -> None:
    text = workflow_text()

    for name in ("DATA_GO_KR_SERVICE_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        assert f"{name}: ${{{{ secrets.{name} }}}}" in text
    assert "if: always()" in text
    assert "actions/upload-artifact@v4" in text


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
