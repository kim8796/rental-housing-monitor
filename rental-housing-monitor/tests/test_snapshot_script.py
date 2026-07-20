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
