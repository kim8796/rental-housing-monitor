#!/usr/bin/env bash
set -Eeuo pipefail
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077

fail() {
    printf '%s\n' "personal monitor backup verification failed" >&2
    exit 1
}

if (( $# != 2 )); then
    fail
fi

ARCHIVE=$1
TARGET=$2

for required_command in python3 sqlite3; do
    command -v "$required_command" >/dev/null 2>&1 || fail
done

python3 - "$ARCHIVE" "$TARGET" <<'PY' || exit 1
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_SIZE = 512 * 1024 * 1024
MAX_MEMBERS = 10_000
MAX_MEMBER_SIZE = 128 * 1024 * 1024
MAX_TOTAL_SIZE = 512 * 1024 * 1024
MAX_PATH_BYTES = 512
MAX_MANIFEST_SIZE = 2 * 1024 * 1024
MAX_COUNT = 10**12

REQUIRED_DIRECTORIES = {
    "data",
    "data/db",
    "data/adaptive",
    "data/vault",
    "secrets",
    "config",
    "manifest",
}
REQUIRED_FILES = {
    "data/db/monitor.db",
    "secrets/master.key",
    "config/compose.yaml",
    "manifest/backup.json",
    "manifest/SHA256SUMS",
}
VARIABLE_PREFIXES = ("data/adaptive/", "data/vault/")
UNSUPPORTED_RAW_TYPES = {b"x", b"g", b"L", b"K", b"S"}
UNSUPPORTED_TAR_TYPES = {
    tarfile.SYMTYPE,
    tarfile.LNKTYPE,
    tarfile.FIFOTYPE,
    tarfile.CHRTYPE,
    tarfile.BLKTYPE,
    tarfile.GNUTYPE_SPARSE,
}
TIMESTAMP_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{6}Z\Z")
MANIFEST_RE = re.compile(r"\A([0-9a-f]{64})  ([^\r\n]+)\n\Z")
COUNT_RE = re.compile(r"\A(monitors|observations|outbox)=([0-9]+)\Z")


class VerificationFailure(Exception):
    pass


def reject(message: str = "") -> None:
    raise VerificationFailure(message)


def cleanup_target(target: Path) -> None:
    try:
        for child in target.iterdir():
            if child.is_symlink() or not child.is_dir():
                child.unlink()
            else:
                shutil.rmtree(child)
    except OSError:
        pass


def parse_octal(field: bytes) -> int:
    if field and field[0] & 0x80:
        reject()
    stripped = field.rstrip(b"\0 ").lstrip(b" ")
    if not stripped:
        return 0
    if not re.fullmatch(rb"[0-7]+", stripped):
        reject()
    return int(stripped, 8)


def inspect_raw_headers(archive: Path) -> None:
    offset = 0
    zero_blocks = 0
    with archive.open("rb") as stream:
        while offset < archive.stat().st_size:
            header = stream.read(512)
            if len(header) != 512:
                reject()
            offset += 512
            if header == bytes(512):
                zero_blocks += 1
                if zero_blocks == 2:
                    if any(stream.read()):
                        reject()
                    return
                continue
            if zero_blocks:
                reject()
            type_flag = header[156:157] or b"\0"
            if type_flag in UNSUPPORTED_RAW_TYPES:
                reject()
            size = parse_octal(header[124:136])
            if size > MAX_MEMBER_SIZE:
                reject()
            padded = ((size + 511) // 512) * 512
            if offset + padded > archive.stat().st_size:
                reject()
            stream.seek(padded, os.SEEK_CUR)
            offset += padded
    reject()


def normalized_name(name: str) -> str:
    if not name or name.startswith("/") or name.endswith("/"):
        reject()
    if len(name.encode("utf-8")) > MAX_PATH_BYTES:
        reject()
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        reject()
    parts = PurePosixPath(name).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        reject()
    normalized = "/".join(parts)
    if normalized != name:
        reject()
    return normalized


def allowed_directory(name: str) -> bool:
    return name in REQUIRED_DIRECTORIES or name.startswith(VARIABLE_PREFIXES)


def allowed_file(name: str) -> bool:
    return name in REQUIRED_FILES or name.startswith(VARIABLE_PREFIXES)


def open_directory_fd(root_fd: int, components: tuple[str, ...]) -> int:
    descriptor = os.dup(root_fd)
    try:
        for component in components:
            next_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def create_directory(root_fd: int, name: str) -> None:
    parts = PurePosixPath(name).parts
    parent_fd = open_directory_fd(root_fd, parts[:-1])
    try:
        os.mkdir(parts[-1], 0o700, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def create_regular_file(
    archive: tarfile.TarFile, member: tarfile.TarInfo, root_fd: int
) -> None:
    parts = PurePosixPath(member.name).parts
    parent_fd = open_directory_fd(root_fd, parts[:-1])
    try:
        descriptor = os.open(
            parts[-1],
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            source = archive.extractfile(member)
            if source is None:
                reject()
            remaining = member.size
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    reject()
                written = 0
                while written < len(chunk):
                    count = os.write(descriptor, chunk[written:])
                    if count <= 0:
                        reject()
                    written += count
                remaining -= len(chunk)
            if source.read(1):
                reject()
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def strict_manifest(target: Path, regular_names: set[str]) -> None:
    manifest_path = target / "manifest" / "SHA256SUMS"
    manifest_bytes = manifest_path.read_bytes()
    if len(manifest_bytes) > MAX_MANIFEST_SIZE or not manifest_bytes.endswith(b"\n"):
        reject()
    try:
        manifest_text = manifest_bytes.decode("utf-8", "strict")
    except UnicodeDecodeError:
        reject()

    entries: list[tuple[str, str]] = []
    for line in manifest_text.splitlines(keepends=True):
        match = MANIFEST_RE.fullmatch(line)
        if match is None:
            reject()
        digest, name = match.groups()
        name = normalized_name(name)
        if not allowed_file(name) or name == "manifest/SHA256SUMS":
            reject()
        entries.append((name, digest))

    names = [name for name, _ in entries]
    if names != sorted(names) or len(names) != len(set(names)):
        reject()
    if set(names) != regular_names - {"manifest/SHA256SUMS"}:
        reject()
    for name, expected_digest in entries:
        path = target.joinpath(*PurePosixPath(name).parts)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_digest:
            reject()


def strict_metadata(target: Path) -> str:
    metadata_path = target / "manifest" / "backup.json"
    try:
        metadata_text = metadata_path.read_text(encoding="utf-8")
        metadata = json.loads(metadata_text)
    except (OSError, UnicodeError, json.JSONDecodeError):
        reject()
    if type(metadata) is not dict or set(metadata) != {
        "archive_timestamp",
        "schema_version",
    }:
        reject()
    if type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1:
        reject()
    timestamp = metadata["archive_timestamp"]
    if type(timestamp) is not str or TIMESTAMP_RE.fullmatch(timestamp) is None:
        reject()
    try:
        parsed = dt.datetime.strptime(timestamp, "%Y-%m-%dT%H%M%SZ")
    except ValueError:
        reject()
    if parsed.strftime("%Y-%m-%dT%H%M%SZ") != timestamp:
        reject()
    canonical = json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n"
    if metadata_text != canonical:
        reject()
    return timestamp


def sqlite_counts(target: Path) -> list[str]:
    database = target / "data" / "db" / "monitor.db"
    query = """
PRAGMA trusted_schema=OFF;
PRAGMA integrity_check;
SELECT 'monitors=' || COUNT(*) FROM "monitors";
SELECT 'observations=' || COUNT(*) FROM "observations";
SELECT 'outbox=' || COUNT(*) FROM "outbox";
"""
    try:
        result = subprocess.run(
            ["sqlite3", "-batch", "-noheader", str(database)],
            input=query,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        reject()
    lines = result.stdout.splitlines()
    if result.returncode != 0 or result.stderr or len(lines) != 4 or lines[0] != "ok":
        reject()
    counts = lines[1:]
    expected_labels = ["monitors", "observations", "outbox"]
    for expected_label, line in zip(expected_labels, counts):
        match = COUNT_RE.fullmatch(line)
        if match is None or match.group(1) != expected_label:
            reject()
        if int(match.group(2)) > MAX_COUNT:
            reject()
    return counts


def verify(archive_path: str, target_path: str) -> tuple[list[str], str]:
    archive = Path(archive_path)
    target = Path(target_path)
    try:
        archive_stat = archive.lstat()
        target_stat = target.lstat()
    except OSError:
        reject()
    if (
        stat.S_ISLNK(archive_stat.st_mode)
        or not stat.S_ISREG(archive_stat.st_mode)
        or archive_stat.st_size <= 0
        or archive_stat.st_size > MAX_ARCHIVE_SIZE
    ):
        reject()
    if (
        stat.S_ISLNK(target_stat.st_mode)
        or not stat.S_ISDIR(target_stat.st_mode)
        or stat.S_IMODE(target_stat.st_mode) != 0o700
        or target_stat.st_uid != os.geteuid()
        or any(target.iterdir())
    ):
        reject()

    inspect_raw_headers(archive)
    names: set[str] = set()
    directories: set[str] = set()
    regular_names: set[str] = set()
    total_size = 0
    with tarfile.open(archive, mode="r:") as tar:
        members = tar.getmembers()
        if not members or len(members) > MAX_MEMBERS or tar.pax_headers:
            reject()
        for member in members:
            name = normalized_name(member.name)
            if name in names:
                duplicates = True
                reject(str(duplicates))
            names.add(name)
            if member.pax_headers or member.sparse is not None:
                reject()
            if member.type in UNSUPPORTED_TAR_TYPES:
                reject()
            if member.isdir():
                if not allowed_directory(name):
                    reject()
                directories.add(name)
            elif member.isreg():
                if not allowed_file(name) or member.size > MAX_MEMBER_SIZE:
                    reject()
                total_size += member.size
                if total_size > MAX_TOTAL_SIZE:
                    reject()
                regular_names.add(name)
            else:
                reject()
        if not REQUIRED_DIRECTORIES.issubset(directories):
            reject()
        if not REQUIRED_FILES.issubset(regular_names):
            reject()

        root_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in sorted(directories, key=lambda value: (value.count("/"), value)):
                create_directory(root_fd, name)
            by_name = {member.name: member for member in members if member.isreg()}
            for name in sorted(regular_names):
                create_regular_file(tar, by_name[name], root_fd)
        finally:
            os.close(root_fd)

    strict_manifest(target, regular_names)
    timestamp = strict_metadata(target)
    counts = sqlite_counts(target)
    for path in target.rglob("*"):
        mode = 0o700 if path.is_dir() else 0o600
        path.chmod(mode)
    return counts, timestamp


archive_argument = sys.argv[1]
target_argument = Path(sys.argv[2])
try:
    safe_counts, safe_timestamp = verify(archive_argument, str(target_argument))
except BaseException:
    cleanup_target(target_argument)
    print("personal monitor backup verification failed", file=sys.stderr)
    raise SystemExit(1)

for count in safe_counts:
    print(count)
print(f"archive_timestamp={safe_timestamp}")
PY
