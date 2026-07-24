from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BACKUP = ROOT / "scripts" / "backup_personal_monitor.sh"
RESTORE = ROOT / "scripts" / "restore_personal_monitor.sh"
VERIFY = ROOT / "scripts" / "verify_backup.sh"
LIFECYCLE = ROOT / "deploy" / "gcs-lifecycle.json"
ARCHIVE_TIMESTAMP = "2026-07-18T193456Z"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.fixture
def backup_text() -> str:
    return _text(BACKUP)


@pytest.fixture
def restore_text() -> str:
    return _text(RESTORE)


@pytest.fixture
def verify_text() -> str:
    return _text(VERIFY)


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _create_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE monitors (id INTEGER PRIMARY KEY);
            CREATE TABLE observations (id INTEGER PRIMARY KEY);
            CREATE TABLE outbox (id INTEGER PRIMARY KEY);
            INSERT INTO monitors DEFAULT VALUES;
            INSERT INTO observations DEFAULT VALUES;
            INSERT INTO observations DEFAULT VALUES;
            """
        )
        connection.commit()
    finally:
        connection.close()
    path.chmod(0o600)


def _valid_archive_files() -> dict[str, bytes]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            """
            CREATE TABLE monitors (id INTEGER PRIMARY KEY);
            CREATE TABLE observations (id INTEGER PRIMARY KEY);
            CREATE TABLE outbox (id INTEGER PRIMARY KEY);
            INSERT INTO monitors DEFAULT VALUES;
            INSERT INTO observations DEFAULT VALUES;
            INSERT INTO observations DEFAULT VALUES;
            """
        )
        connection.commit()
        database = connection.serialize()
    finally:
        connection.close()

    files = {
        "data/db/monitor.db": database,
        "secrets/master.key": b"encrypted-master-key\n",
        "config/compose.yaml": b"services: {}\n",
        "manifest/backup.json": (
            b'{"archive_timestamp":"2026-07-18T193456Z","schema_version":1}\n'
        ),
    }
    manifest = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {name}\n"
        for name, content in sorted(files.items())
    )
    files["manifest/SHA256SUMS"] = manifest.encode()
    return files


def _write_archive(
    path: Path,
    *,
    files: dict[str, bytes] | None = None,
    directories: list[str] | None = None,
    extra_members: list[tuple[tarfile.TarInfo, bytes]] | None = None,
    archive_format: int = tarfile.GNU_FORMAT,
) -> None:
    archive_files = _valid_archive_files() if files is None else files
    archive_directories = directories or [
        "data",
        "data/db",
        "data/adaptive",
        "data/vault",
        "secrets",
        "config",
        "manifest",
    ]
    with tarfile.open(path, "w", format=archive_format) as archive:
        for name in archive_directories:
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = 0o700
            archive.addfile(member)
        for name, content in sorted(archive_files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(content))
        for member, content in extra_members or []:
            archive.addfile(member, io.BytesIO(content) if content else None)


def _run_verifier(tmp_path: Path, archive: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    target = tmp_path / "verified"
    target.mkdir(mode=0o700)
    result = subprocess.run(
        [str(VERIFY), str(archive), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    return result, target


def _private_script_copy(source: Path, destination: Path, replacements: dict[str, str]) -> Path:
    text = source.read_text(encoding="utf-8")
    for old, new in replacements.items():
        assert old in text
        text = text.replace(old, new)
    destination.write_text(text, encoding="utf-8")
    destination.chmod(0o700)
    return destination


def test_required_backup_artifacts_exist() -> None:
    assert all(path.is_file() for path in (BACKUP, RESTORE, VERIFY, LIFECYCLE))


def test_scripts_use_strict_private_shell_baseline(
    backup_text: str, restore_text: str, verify_text: str
) -> None:
    for script in (backup_text, restore_text, verify_text):
        assert script.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail\n")
        assert "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" in script
        assert "export PATH" in script
        assert "umask 077" in script
        assert "set -x" not in script
        assert "printenv" not in script
        assert "\nenv\n" not in script
        assert "$*" not in script


def test_backup_can_find_gcloud_snap_on_ubuntu_gce(backup_text: str) -> None:
    assert ":/snap/bin" in backup_text


def test_backup_has_fixed_allowlist_and_forbidden_exclusions(backup_text: str) -> None:
    for required in (
        'SOURCE_ROOT="/srv/personal-monitor"',
        'DATABASE="/srv/personal-monitor/db/monitor.db"',
        'ADAPTIVE="/srv/personal-monitor/adaptive"',
        'VAULT="/srv/personal-monitor/vault"',
        'MASTER_KEY="/etc/personal-monitor/master.key"',
        'COMPOSE_CONFIG="/srv/personal-monitor/app/compose.yaml"',
        'STATUS_FILE="/srv/personal-monitor/logs/backup-status.json"',
    ):
        assert required in backup_text
    for forbidden in (
        "/srv/personal-monitor/.env",
        "/srv/personal-monitor/diagnostics",
        "/srv/personal-monitor/codex-home",
        "auth.json",
        "TELEGRAM_BOT_TOKEN",
        "DATA_GO_KR_SERVICE_KEY",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "monitor.db-wal",
        "monitor.db-shm",
        "monitor.db-journal",
    ):
        assert forbidden not in backup_text


def test_backup_uses_consistent_sqlite_copy_before_verification_and_encryption(
    backup_text: str,
) -> None:
    sqlite_backup = 'sqlite3 "$DATABASE" ".backup'
    assert sqlite_backup in backup_text
    assert '"$SCRIPT_DIR/verify_backup.sh"' in backup_text
    assert 'age --recipient "$AGE_RECIPIENT"' in backup_text
    assert backup_text.index(sqlite_backup) < backup_text.rindex('"$SCRIPT_DIR/verify_backup.sh"')
    assert backup_text.rindex('"$SCRIPT_DIR/verify_backup.sh"') < backup_text.index(
        'age --recipient "$AGE_RECIPIENT"'
    )
    assert 'cp "$DATABASE"' not in backup_text


def test_backup_uses_lock_private_workspaces_and_one_failure_trap(
    backup_text: str,
) -> None:
    assert 'LOCK_DIRECTORY="/run/lock/personal-monitor-backup"' in backup_text
    assert 'LOCK_FILE="$LOCK_DIRECTORY/backup.lock"' in backup_text
    assert "flock --exclusive --nonblock" in backup_text
    assert "mktemp -d" in backup_text
    assert "chmod 0700" in backup_text
    assert backup_text.count("trap ") == 1
    assert "trap cleanup EXIT" in backup_text
    assert "mktemp /" not in backup_text


def test_backup_rejects_links_and_special_files_without_mutating_live_tree(
    backup_text: str,
) -> None:
    assert "is_symlink()" in backup_text
    assert "S_ISREG" in backup_text
    assert "S_ISDIR" in backup_text
    assert "O_NOFOLLOW" in backup_text
    assert "chmod -R" not in backup_text
    assert "chown -R" not in backup_text


def test_backup_uses_deterministic_tar_and_closed_manifest(backup_text: str) -> None:
    for flag in (
        "--create",
        "--sort=name",
        "--mtime=@0",
        "--owner=0",
        "--group=0",
        "--numeric-owner",
        "--format=gnu",
    ):
        assert flag in backup_text
    assert "manifest/SHA256SUMS" in backup_text
    assert "manifest/backup.json" in backup_text
    assert "sorted(" in backup_text
    assert "hashlib.sha256" in backup_text


def test_backup_uploads_create_only_and_verifies_remote_metadata(
    backup_text: str,
) -> None:
    assert "daily/${ARCHIVE_TIMESTAMP}.tar.age" in backup_text
    assert "weekly/${ARCHIVE_TIMESTAMP}.tar.age" in backup_text
    assert 'date -u "+%Y-%m-%dT%H%M%SZ"' in backup_text
    assert backup_text.count('date -u "+%Y-%m-%dT%H%M%SZ"') == 1
    assert 'ZoneInfo("Asia/Seoul")' in backup_text
    assert "astimezone" in backup_text
    assert 'date -u "+%Y-%m-%dT%H%M%SZ %u"' not in backup_text
    assert "--if-generation-match=0" in backup_text
    assert "--custom-metadata" in backup_text
    assert "--content-type=application/octet-stream" in backup_text
    assert "gcloud storage objects describe" in backup_text
    assert "--raw" in backup_text
    assert "--format=json(size,metadata)" in backup_text
    assert "sha256" in backup_text


def test_backup_retention_is_bounded_and_validated_before_deletion(
    backup_text: str,
) -> None:
    assert '"daily" "7"' in backup_text
    assert '"weekly" "4"' in backup_text
    assert "gcloud storage objects list" in backup_text
    assert "--format=json(name,generation)" in backup_text
    assert "fullmatch(" in backup_text
    assert backup_text.index("validate_retention_listing") < backup_text.index("gcloud storage rm")
    assert '--if-generation-match="$object_generation"' in backup_text


def test_backup_suppresses_cloud_command_output_and_streams_local_hash(
    backup_text: str,
) -> None:
    assert "gcloud storage cp \\\n" in backup_text
    assert backup_text.count("2>/dev/null") + backup_text.count("2>&1") >= 6
    assert "for chunk in iter(" in backup_text
    assert "archive.read_bytes()" not in backup_text


def test_backup_status_is_atomic_closed_and_service_owned(backup_text: str) -> None:
    assert '"schema_version":1' in backup_text
    assert '"status":"%s"' in backup_text
    assert '"updated_at":"%s"' in backup_text
    assert "chmod 0600" in backup_text
    assert "chown 10001:10001" in backup_text
    assert "mv -f --" in backup_text
    assert '"error":' not in backup_text
    assert "printenv" not in backup_text


def test_restore_contract_and_nonempty_marker(restore_text: str) -> None:
    assert "if (( $# != 3 )); then" in restore_text
    assert "TARGET_MUST_BE_EMPTY" in restore_text
    assert 'age --decrypt --identity "$IDENTITY"' in restore_text
    assert '"$SCRIPT_DIR/verify_backup.sh"' in restore_text
    assert "/srv/personal-monitor" not in restore_text
    assert "compose" not in restore_text.lower()
    assert "gcloud" not in restore_text


def test_restore_uses_sibling_verification_and_partial_cleanup(restore_text: str) -> None:
    assert "mktemp -d" in restore_text
    assert "TARGET_DEVICE" in restore_text
    assert "TARGET_INODE" in restore_text
    assert "target_is_still_dedicated" in restore_text
    assert "remove_target_children" in restore_text
    assert "find" in restore_text
    assert "chmod 0700" in restore_text
    assert "chmod 0600" in restore_text


def test_verifier_has_explicit_untrusted_tar_limits(verify_text: str) -> None:
    for constant in (
        "MAX_ARCHIVE_SIZE",
        "MAX_MEMBERS",
        "MAX_MEMBER_SIZE",
        "MAX_TOTAL_SIZE",
        "MAX_PATH_BYTES",
    ):
        assert constant in verify_text
    for tar_type in ("SYMTYPE", "LNKTYPE", "FIFOTYPE", "CHRTYPE", "BLKTYPE"):
        assert tar_type in verify_text
    assert "pax_headers" in verify_text
    assert "duplicates" in verify_text
    assert "O_NOFOLLOW" in verify_text
    assert "while written < len(chunk)" in verify_text
    assert "PRAGMA integrity_check" in verify_text


def test_lifecycle_json_is_exact() -> None:
    assert json.loads(_text(LIFECYCLE)) == {
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


def test_verifier_accepts_closed_archive_and_prints_only_safe_counts(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "backup.tar"
    _write_archive(archive)
    result, target = _run_verifier(tmp_path, archive)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        "monitors=1",
        "observations=2",
        "outbox=0",
        f"archive_timestamp={ARCHIVE_TIMESTAMP}",
    ]
    assert (target / "data" / "adaptive").is_dir()
    assert (target / "data" / "vault").is_dir()
    for path in target.rglob("*"):
        expected = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == expected


ArchiveMutation = Callable[
    [dict[str, bytes], list[str], list[tuple[tarfile.TarInfo, bytes]]],
    tuple[dict[str, bytes], list[str], list[tuple[tarfile.TarInfo, bytes]], int],
]


def _path_member(name: str) -> ArchiveMutation:
    def mutate(
        files: dict[str, bytes],
        directories: list[str],
        members: list[tuple[tarfile.TarInfo, bytes]],
    ) -> tuple[dict[str, bytes], list[str], list[tuple[tarfile.TarInfo, bytes]], int]:
        member = tarfile.TarInfo(name)
        content = b"bad"
        member.size = len(content)
        members.append((member, content))
        return files, directories, members, tarfile.GNU_FORMAT

    return mutate


def _special_member(member_type: bytes) -> ArchiveMutation:
    def mutate(
        files: dict[str, bytes],
        directories: list[str],
        members: list[tuple[tarfile.TarInfo, bytes]],
    ) -> tuple[dict[str, bytes], list[str], list[tuple[tarfile.TarInfo, bytes]], int]:
        member = tarfile.TarInfo("data/adaptive/special")
        member.type = member_type
        member.linkname = "data/db/monitor.db"
        members.append((member, b""))
        return files, directories, members, tarfile.GNU_FORMAT

    return mutate


def _duplicate_member(
    files: dict[str, bytes],
    directories: list[str],
    members: list[tuple[tarfile.TarInfo, bytes]],
) -> tuple[dict[str, bytes], list[str], list[tuple[tarfile.TarInfo, bytes]], int]:
    member = tarfile.TarInfo("config/compose.yaml")
    member.size = 3
    members.append((member, b"bad"))
    return files, directories, members, tarfile.GNU_FORMAT


def _pax_archive(
    files: dict[str, bytes],
    directories: list[str],
    members: list[tuple[tarfile.TarInfo, bytes]],
) -> tuple[dict[str, bytes], list[str], list[tuple[tarfile.TarInfo, bytes]], int]:
    member = tarfile.TarInfo("config/compose.yaml")
    member.size = 13
    member.pax_headers = {"comment": "forbidden"}
    members.append((member, b"services: {}\n"))
    return files, directories, members, tarfile.PAX_FORMAT


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(_path_member("../escape"), id="traversal"),
        pytest.param(_path_member("/absolute"), id="absolute"),
        pytest.param(_path_member("unexpected/file"), id="unexpected-root"),
        pytest.param(_duplicate_member, id="duplicate"),
        pytest.param(_special_member(tarfile.SYMTYPE), id="symlink"),
        pytest.param(_special_member(tarfile.LNKTYPE), id="hardlink"),
        pytest.param(_special_member(tarfile.FIFOTYPE), id="fifo"),
        pytest.param(_special_member(tarfile.CHRTYPE), id="device"),
        pytest.param(_pax_archive, id="pax"),
    ],
)
def test_verifier_rejects_malicious_tar_members(tmp_path: Path, mutation: ArchiveMutation) -> None:
    files = _valid_archive_files()
    directories = [
        "data",
        "data/db",
        "data/adaptive",
        "data/vault",
        "secrets",
        "config",
        "manifest",
    ]
    files, directories, members, archive_format = mutation(files, directories, [])
    archive = tmp_path / "malicious.tar"
    _write_archive(
        archive,
        files=files,
        directories=directories,
        extra_members=members,
        archive_format=archive_format,
    )
    result, target = _run_verifier(tmp_path, archive)
    assert result.returncode != 0
    assert result.stdout == ""
    assert list(target.iterdir()) == []


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda files: files.pop("manifest/SHA256SUMS"), id="missing-manifest"),
        pytest.param(
            lambda files: files.__setitem__(
                "manifest/SHA256SUMS",
                files["manifest/SHA256SUMS"] + b"0" * 64 + b"  data/vault/extra\n",
            ),
            id="extra-checksum",
        ),
        pytest.param(
            lambda files: files.__setitem__(
                "manifest/SHA256SUMS", b"not-a-digest data/db/monitor.db\n"
            ),
            id="malformed-manifest",
        ),
        pytest.param(
            lambda files: files.__setitem__("config/compose.yaml", b"tampered after manifest"),
            id="hash-mismatch",
        ),
    ],
)
def test_verifier_rejects_manifest_failures(
    tmp_path: Path, mutation: Callable[[dict[str, bytes]], object]
) -> None:
    files = _valid_archive_files()
    mutation(files)
    archive = tmp_path / "bad-manifest.tar"
    _write_archive(archive, files=files)
    result, target = _run_verifier(tmp_path, archive)
    assert result.returncode != 0
    assert result.stdout == ""
    assert list(target.iterdir()) == []


def test_verifier_rejects_oversized_input_before_extraction(tmp_path: Path) -> None:
    archive = tmp_path / "oversized.tar"
    with archive.open("wb") as stream:
        stream.truncate(600 * 1024 * 1024)
    result, target = _run_verifier(tmp_path, archive)
    assert result.returncode != 0
    assert result.stdout == ""
    assert list(target.iterdir()) == []


def test_verifier_rejects_nonzero_trailing_archive_data(tmp_path: Path) -> None:
    archive = tmp_path / "trailing-data.tar"
    _write_archive(archive)
    with archive.open("ab") as stream:
        stream.write(b"nonzero trailing bytes")
    result, target = _run_verifier(tmp_path, archive)
    assert result.returncode != 0
    assert result.stdout == ""
    assert list(target.iterdir()) == []


def _prepare_restore_harness(tmp_path: Path, archive: Path) -> tuple[Path, Path, Path]:
    harness = tmp_path / "restore-harness"
    harness.mkdir(mode=0o700)
    command_dir = harness / "bin"
    command_dir.mkdir(mode=0o700)
    shutil.copy2(VERIFY, harness / VERIFY.name)
    (harness / VERIFY.name).chmod(0o700)
    fake_age = command_dir / "age"
    _write_executable(
        fake_age,
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "FAKE_RESTORE_SENSITIVE" >&2
output=
archive=
while (( $# )); do
    case "$1" in
        --output) output=$2; shift 2 ;;
        --identity) shift 2 ;;
        --decrypt) shift ;;
        *) archive=$1; shift ;;
    esac
done
/bin/cp "$archive" "$output"
""",
    )
    restore = _private_script_copy(
        RESTORE,
        harness / RESTORE.name,
        {
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin": (
                f"PATH={command_dir}:/usr/bin:/bin"
            )
        },
    )
    identity = tmp_path / "identity.txt"
    identity.write_text("fake test identity\n", encoding="utf-8")
    identity.chmod(0o600)
    archive.chmod(0o600)
    return restore, identity, command_dir


def test_restore_accepts_valid_archive_into_empty_private_target(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "backup.tar.age"
    _write_archive(archive)
    restore, identity, _ = _prepare_restore_harness(tmp_path, archive)
    target = tmp_path / "restore-target"
    target.mkdir(mode=0o700)
    result = subprocess.run(
        [str(restore), str(archive), str(target), str(identity)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        "monitors=1",
        "observations=2",
        "outbox=0",
        f"archive_timestamp={ARCHIVE_TIMESTAMP}",
    ]
    assert (target / "data" / "db" / "monitor.db").is_file()


def test_restore_refuses_nonempty_target_without_decrypting(tmp_path: Path) -> None:
    archive = tmp_path / "backup.tar.age"
    _write_archive(archive)
    restore, identity, command_dir = _prepare_restore_harness(tmp_path, archive)
    marker = command_dir / "age-called"
    age = command_dir / "age"
    age.write_text(f"#!/usr/bin/env bash\nset -eu\n: > {marker!s}\nexit 99\n", encoding="utf-8")
    age.chmod(0o755)
    target = tmp_path / "restore-target"
    target.mkdir(mode=0o700)
    (target / "existing").write_text("operator data", encoding="utf-8")
    result = subprocess.run(
        [str(restore), str(archive), str(target), str(identity)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "TARGET_MUST_BE_EMPTY" in result.stderr
    assert not marker.exists()
    assert (target / "existing").read_text(encoding="utf-8") == "operator data"


def test_restore_cleans_partial_move_failure(tmp_path: Path) -> None:
    archive = tmp_path / "backup.tar.age"
    _write_archive(archive)
    restore, identity, command_dir = _prepare_restore_harness(tmp_path, archive)
    fake_mv = command_dir / "mv"
    counter = tmp_path / "mv-count"
    _write_executable(
        fake_mv,
        f"""#!/usr/bin/env bash
set -eu
count=0
if [[ -f {counter!s} ]]; then count=$(/bin/cat {counter!s}); fi
count=$((count + 1))
printf '%s' "$count" > {counter!s}
if (( count == 2 )); then exit 88; fi
exec /bin/mv "$@"
""",
    )
    target = tmp_path / "restore-target"
    target.mkdir(mode=0o700)
    result = subprocess.run(
        [str(restore), str(archive), str(target), str(identity)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert list(target.iterdir()) == []


def test_restore_cleans_target_when_safe_output_cannot_be_printed(tmp_path: Path) -> None:
    archive = tmp_path / "backup.tar.age"
    _write_archive(archive)
    restore, identity, command_dir = _prepare_restore_harness(tmp_path, archive)
    _write_executable(command_dir / "cat", "#!/usr/bin/env bash\nexit 77\n")
    target = tmp_path / "restore-target"
    target.mkdir(mode=0o700)
    result = subprocess.run(
        [str(restore), str(archive), str(target), str(identity)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert list(target.iterdir()) == []


def _prepare_backup_source(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "srv" / "personal-monitor"
    for relative in ("db", "adaptive", "vault", "logs", "app"):
        (source / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
        (source / relative).chmod(0o700)
    source.chmod(0o700)
    _create_database(source / "db" / "monitor.db")
    (source / "adaptive" / "state.json").write_text("{}", encoding="utf-8")
    (source / "adaptive" / "state.json").chmod(0o600)
    (source / "vault" / "record.age").write_text("encrypted", encoding="utf-8")
    (source / "vault" / "record.age").chmod(0o600)
    (source / "app" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (source / "app" / "compose.yaml").chmod(0o644)
    master_key = tmp_path / "etc" / "personal-monitor" / "master.key"
    master_key.parent.mkdir(parents=True, mode=0o700)
    master_key.write_text("encrypted-master-key\n", encoding="utf-8")
    master_key.chmod(0o600)
    return source, master_key


def _prepare_backup_harness(
    tmp_path: Path,
    *,
    malformed_listing: bool = False,
    failing_chown: bool = False,
    missing_age: bool = False,
) -> tuple[Path, Path, dict[str, str]]:
    source, master_key = _prepare_backup_source(tmp_path)
    harness = tmp_path / "backup-harness"
    harness.mkdir(mode=0o700)
    command_dir = harness / "bin"
    command_dir.mkdir(mode=0o700)
    event_log = tmp_path / "events.log"
    remote = tmp_path / "remote"
    remote.mkdir(mode=0o700)

    _write_executable(
        command_dir / "flock",
        "#!/usr/bin/env bash\nset -eu\nprintf '%s\\n' flock >> \"$FAKE_EVENT_LOG\"\n",
    )
    _write_executable(
        command_dir / "chown",
        (
            "#!/usr/bin/env bash\nset -eu\n"
            "printf '%s\\n' chown >> \"$FAKE_EVENT_LOG\"\n"
            f"exit {73 if failing_chown else 0}\n"
        ),
    )
    _write_executable(
        command_dir / "date",
        """#!/usr/bin/env bash
set -eu
case "${*: -1}" in
  "+%Y-%m-%dT%H%M%SZ") printf '%s\n' "2026-07-18T193456Z" ;;
  *) exit 2 ;;
esac
""",
    )
    _write_executable(
        command_dir / "sqlite3",
        """#!/usr/bin/env python3
import os
import pathlib
import re
import shutil
import sys

with open(os.environ["FAKE_EVENT_LOG"], "a", encoding="utf-8") as log:
    log.write("sqlite-backup\\n")
print("FAKE_SOURCE_SENSITIVE", file=sys.stderr)
match = re.fullmatch(r"\\.backup '([^']+)'", sys.argv[2])
if match is None:
    raise SystemExit(2)
shutil.copyfile(sys.argv[1], pathlib.Path(match.group(1)))
""",
    )
    _write_executable(
        command_dir / "tar",
        """#!/usr/bin/env python3
import os
import pathlib
import sys
import tarfile

with open(os.environ["FAKE_EVENT_LOG"], "a", encoding="utf-8") as log:
    log.write("tar\\n")
print("FAKE_SOURCE_SENSITIVE", file=sys.stderr)
root = pathlib.Path(sys.argv[sys.argv.index("-C") + 1])
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.GNU_FORMAT) as archive:
    for top in ("config", "data", "manifest", "secrets"):
        for path in [root / top, *sorted((root / top).rglob("*"))]:
            archive.add(path, arcname=path.relative_to(root), recursive=False)
""",
    )
    if not missing_age:
        _write_executable(
            command_dir / "age",
            """#!/usr/bin/env python3
import os
import pathlib
import sys

with open(os.environ["FAKE_EVENT_LOG"], "a", encoding="utf-8") as log:
    log.write("age\\n")
print("FAKE_SOURCE_SENSITIVE", file=sys.stderr)
output = pathlib.Path(sys.argv[sys.argv.index("--output") + 1])
output.write_bytes(sys.stdin.buffer.read())
""",
        )
    malformed = "True" if malformed_listing else "False"
    _write_executable(
        command_dir / "gcloud",
        f"""#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import shutil
import sys

args = sys.argv[1:]
print("FAKE_GCLOUD_SENSITIVE", file=sys.stderr)
with open(os.environ["FAKE_EVENT_LOG"], "a", encoding="utf-8") as log:
    log.write("gcloud " + " ".join(args) + "\\n")
remote = pathlib.Path(os.environ["FAKE_REMOTE"])
if args[:2] == ["storage", "cp"]:
    source = pathlib.Path(args[2])
    name = args[3].split("/", 3)[-1]
    destination = remote / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
elif args[:3] == ["storage", "objects", "describe"]:
    name = args[3].split("/", 3)[-1]
    content = (remote / name).read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    metadata = {{"sha256": digest, "size": str(len(content))}}
    override = os.environ.get("FAKE_DESCRIBE_JSON")
    if override == "duplicate":
        print(
            '{{"size":"1","size":"'
            + str(len(content))
            + '","metadata":'
            + json.dumps(metadata)
            + '}}'
        )
    elif override is not None:
        print(override)
    else:
        print(
            json.dumps(
                {{"size": str(len(content)), "metadata": metadata}}
            )
        )
elif args[:3] == ["storage", "objects", "list"]:
    if {malformed} and "weekly" in args[3]:
        print('{{malformed')
    else:
        key = "FAKE_WEEKLY_LISTING" if "weekly" in args[3] else "FAKE_DAILY_LISTING"
        print(os.environ.get(key, "[]"))
elif args[:2] == ["storage", "rm"]:
    raise SystemExit(0)
else:
    raise SystemExit(3)
""",
    )
    shutil.copy2(VERIFY, harness / VERIFY.name)
    (harness / VERIFY.name).chmod(0o700)
    replacements = {
        'SOURCE_ROOT="/srv/personal-monitor"': f'SOURCE_ROOT="{source}"',
        'DATABASE="/srv/personal-monitor/db/monitor.db"': (
            f'DATABASE="{source / "db" / "monitor.db"}"'
        ),
        'ADAPTIVE="/srv/personal-monitor/adaptive"': (f'ADAPTIVE="{source / "adaptive"}"'),
        'VAULT="/srv/personal-monitor/vault"': f'VAULT="{source / "vault"}"',
        'MASTER_KEY="/etc/personal-monitor/master.key"': f'MASTER_KEY="{master_key}"',
        'COMPOSE_CONFIG="/srv/personal-monitor/app/compose.yaml"': (
            f'COMPOSE_CONFIG="{source / "app" / "compose.yaml"}"'
        ),
        'STATUS_FILE="/srv/personal-monitor/logs/backup-status.json"': (
            f'STATUS_FILE="{source / "logs" / "backup-status.json"}"'
        ),
        'LOCK_DIRECTORY="/run/lock/personal-monitor-backup"': (f'LOCK_DIRECTORY="{tmp_path}"'),
        'LOCK_FILE="$LOCK_DIRECTORY/backup.lock"': (f'LOCK_FILE="{tmp_path / "backup.lock"}"'),
        "SERVICE_UID=10001": f"SERVICE_UID={os.getuid()}",
        "SERVICE_GID=10001": f"SERVICE_GID={os.getgid()}",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin": (
            f"PATH={command_dir}:/usr/bin:/bin"
        ),
    }
    backup = _private_script_copy(BACKUP, harness / BACKUP.name, replacements)
    environment = {
        **os.environ,
        "AGE_RECIPIENT": "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqd3c4p",
        "PERSONAL_MONITOR_BACKUP_BUCKET": "gs://personal-monitor-test",
        "FAKE_EVENT_LOG": str(event_log),
        "FAKE_REMOTE": str(remote),
    }
    return backup, source, environment


def test_backup_fake_command_flow_is_ordered_verified_and_weekly(
    tmp_path: Path,
) -> None:
    backup, source, environment = _prepare_backup_harness(tmp_path)
    result = subprocess.run(
        [str(backup)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    events = Path(environment["FAKE_EVENT_LOG"]).read_text(encoding="utf-8")
    assert events.index("sqlite-backup") < events.index("tar") < events.index("age")
    assert "daily/2026-07-18T193456Z.tar.age" in events
    assert "weekly/2026-07-18T193456Z.tar.age" in events
    assert events.count("--if-generation-match=0") == 2
    assert events.count("storage objects describe") == 2
    assert result.stdout.splitlines() == [
        "personal monitor backup succeeded",
        "2026-07-18T193456Z",
    ]
    assert result.stderr == ""
    status = source / "logs" / "backup-status.json"
    assert json.loads(status.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "status": "ok",
        "updated_at": ARCHIVE_TIMESTAMP,
    }
    assert stat.S_IMODE(status.stat().st_mode) == 0o600


def test_backup_malformed_retention_aborts_before_deletion_and_writes_failure(
    tmp_path: Path,
) -> None:
    backup, source, environment = _prepare_backup_harness(tmp_path, malformed_listing=True)
    result = subprocess.run(
        [str(backup)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    events = Path(environment["FAKE_EVENT_LOG"]).read_text(encoding="utf-8")
    assert "storage rm" not in events
    status = json.loads((source / "logs" / "backup-status.json").read_text(encoding="utf-8"))
    assert status == {
        "schema_version": 1,
        "status": "failed",
        "updated_at": ARCHIVE_TIMESTAMP,
    }
    combined = result.stdout + result.stderr
    assert "personal-monitor-test" not in combined
    assert environment["AGE_RECIPIENT"] not in combined


def test_backup_retention_keeps_seven_and_four_with_generation_guards(
    tmp_path: Path,
) -> None:
    backup, _, environment = _prepare_backup_harness(tmp_path)
    daily = [
        {
            "name": f"daily/2026-07-{day:02d}T123456Z.tar.age",
            "generation": str(1000 + day),
        }
        for day in range(10, 19)
    ]
    weekly = [
        {
            "name": f"weekly/2026-06-{day:02d}T123456Z.tar.age",
            "generation": str(2000 + day),
        }
        for day in range(1, 7)
    ]
    environment["FAKE_DAILY_LISTING"] = json.dumps(daily)
    environment["FAKE_WEEKLY_LISTING"] = json.dumps(weekly)
    result = subprocess.run(
        [str(backup)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    events = Path(environment["FAKE_EVENT_LOG"]).read_text(encoding="utf-8")
    removals = [line for line in events.splitlines() if "storage rm" in line]
    assert len([line for line in removals if "/daily/" in line]) == 2
    assert len([line for line in removals if "/weekly/" in line]) == 2
    assert all("--if-generation-match=" in line for line in removals)


def test_backup_retention_rejects_impossible_calendar_date_before_delete(
    tmp_path: Path,
) -> None:
    backup, _, environment = _prepare_backup_harness(tmp_path)
    environment["FAKE_DAILY_LISTING"] = json.dumps(
        [
            {
                "name": "daily/2026-02-31T123456Z.tar.age",
                "generation": "1234",
            }
        ]
    )
    result = subprocess.run(
        [str(backup)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    events = Path(environment["FAKE_EVENT_LOG"]).read_text(encoding="utf-8")
    assert "storage rm" not in events


def test_backup_rejects_duplicate_remote_description_keys(tmp_path: Path) -> None:
    backup, _, environment = _prepare_backup_harness(tmp_path)
    environment["FAKE_DESCRIBE_JSON"] = "duplicate"
    result = subprocess.run(
        [str(backup)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_backup_rejects_duplicate_retention_json_keys(tmp_path: Path) -> None:
    backup, _, environment = _prepare_backup_harness(tmp_path)
    environment["FAKE_DAILY_LISTING"] = (
        '[{"name":"daily/invalid","name":"daily/2026-07-18T123456Z.tar.age","generation":"1234"}]'
    )
    result = subprocess.run(
        [str(backup)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_backup_requires_private_service_source_root(tmp_path: Path) -> None:
    backup, source, environment = _prepare_backup_harness(tmp_path)
    source.chmod(0o755)
    result = subprocess.run(
        [str(backup)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    events = Path(environment["FAKE_EVENT_LOG"]).read_text(encoding="utf-8")
    assert "sqlite-backup" not in events
    assert "gcloud" not in events


@pytest.mark.parametrize("parent_kind", ["compose", "master-key"])
def test_backup_rejects_symlinked_fixed_file_parent(tmp_path: Path, parent_kind: str) -> None:
    backup, source, environment = _prepare_backup_harness(tmp_path)
    parent = source / "app" if parent_kind == "compose" else tmp_path / "etc" / "personal-monitor"
    actual = parent.with_name(f"{parent.name}-actual")
    parent.rename(actual)
    parent.symlink_to(actual, target_is_directory=True)
    result = subprocess.run(
        [str(backup)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    events = Path(environment["FAKE_EVENT_LOG"]).read_text(encoding="utf-8")
    assert "sqlite-backup" not in events
    assert "gcloud" not in events


def test_backup_status_write_failure_cannot_report_success(tmp_path: Path) -> None:
    backup, source, environment = _prepare_backup_harness(tmp_path, failing_chown=True)
    result = subprocess.run(
        [str(backup)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "personal monitor backup succeeded" not in result.stdout
    assert not (source / "logs" / "backup-status.json").exists()


def test_backup_missing_age_still_writes_atomic_failed_status(tmp_path: Path) -> None:
    backup, source, environment = _prepare_backup_harness(tmp_path, missing_age=True)
    result = subprocess.run(
        [str(backup)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert json.loads((source / "logs" / "backup-status.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "status": "failed",
        "updated_at": ARCHIVE_TIMESTAMP,
    }


@pytest.mark.parametrize("script", [BACKUP, RESTORE, VERIFY])
def test_bash_syntax(script: Path) -> None:
    result = subprocess.run(
        ["bash", "-n", str(script)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
