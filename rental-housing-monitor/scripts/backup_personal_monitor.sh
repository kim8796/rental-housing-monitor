#!/usr/bin/env bash
set -Eeuo pipefail
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077

SOURCE_ROOT="/srv/personal-monitor"
DATABASE="/srv/personal-monitor/db/monitor.db"
ADAPTIVE="/srv/personal-monitor/adaptive"
VAULT="/srv/personal-monitor/vault"
MASTER_KEY="/etc/personal-monitor/master.key"
COMPOSE_CONFIG="/srv/personal-monitor/app/compose.yaml"
STATUS_FILE="/srv/personal-monitor/logs/backup-status.json"
LOCK_DIRECTORY="/run/lock/personal-monitor-backup"
LOCK_FILE="$LOCK_DIRECTORY/backup.lock"
SERVICE_UID=10001
SERVICE_GID=10001

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
WORKSPACE=
ARCHIVE_TIMESTAMP=
ARCHIVE_WEEKDAY=
STATUS_READY=0
COMPLETE=0

fail() {
    exit 1
}

write_status() {
    local status=$1
    local status_directory
    local temporary_status
    status_directory=$(dirname -- "$STATUS_FILE")
    temporary_status=$(mktemp "$status_directory/.backup-status.XXXXXXXXXX" 2>/dev/null)
    if ! printf '{"schema_version":1,"status":"%s","updated_at":"%s"}\n' \
        "$status" "$ARCHIVE_TIMESTAMP" >"$temporary_status" ||
        ! chmod 0600 "$temporary_status" 2>/dev/null ||
        ! chown 10001:10001 "$temporary_status" 2>/dev/null ||
        ! mv -f -- "$temporary_status" "$STATUS_FILE" 2>/dev/null; then
        rm -f -- "$temporary_status" >/dev/null 2>&1
        return 1
    fi
}

cleanup() {
    local exit_status=$?
    local outcome=failed
    set +e
    if [[ -n "$WORKSPACE" && -d "$WORKSPACE" && ! -L "$WORKSPACE" ]]; then
        rm -rf -- "$WORKSPACE" >/dev/null 2>&1
        if (( $? != 0 )); then
            exit_status=1
        fi
    fi
    if (( exit_status == 0 && COMPLETE == 1 )); then
        outcome=ok
    fi
    if (( STATUS_READY == 1 )); then
        if ! write_status "$outcome"; then
            outcome=failed
            exit_status=1
            write_status failed >/dev/null 2>&1 || true
        fi
    else
        outcome=failed
        exit_status=1
    fi
    if [[ "$outcome" == ok ]]; then
        printf '%s\n' "personal monitor backup succeeded"
        printf '%s\n' "$ARCHIVE_TIMESTAMP"
        exit 0
    fi
    printf '%s\n' "personal monitor backup failed" >&2
    if [[ "$ARCHIVE_TIMESTAMP" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z$ ]]; then
        printf '%s\n' "$ARCHIVE_TIMESTAMP" >&2
    fi
    exit "${exit_status:-1}"
}
trap cleanup EXIT

for required_command in chmod chown date dirname mktemp mv python3 rm; do
    command -v "$required_command" >/dev/null 2>&1 || fail
done

TIME_SNAPSHOT=$(date -u "+%Y-%m-%dT%H%M%SZ %u")
[[ "$TIME_SNAPSHOT" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z\ [1-7]$ ]] || fail
ARCHIVE_TIMESTAMP=${TIME_SNAPSHOT% *}
ARCHIVE_WEEKDAY=${TIME_SNAPSHOT##* }
[[ "$ARCHIVE_TIMESTAMP" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z$ ]] || fail

python3 - "$STATUS_FILE" "$SERVICE_UID" "$SERVICE_GID" <<'PY' || exit 1
import os
import stat
import sys
from pathlib import Path

status_file = Path(sys.argv[1])
service_uid = int(sys.argv[2])
service_gid = int(sys.argv[3])
directory = status_file.parent
try:
    directory_stat = directory.lstat()
except OSError:
    raise SystemExit(1)
if (
    directory.is_symlink()
    or not stat.S_ISDIR(directory_stat.st_mode)
    or directory_stat.st_uid != service_uid
    or directory_stat.st_gid != service_gid
    or stat.S_IMODE(directory_stat.st_mode) != 0o700
):
    raise SystemExit(1)
if status_file.exists() or status_file.is_symlink():
    try:
        status_stat = status_file.lstat()
    except OSError:
        raise SystemExit(1)
    if (
        status_file.is_symlink()
        or not stat.S_ISREG(status_stat.st_mode)
        or status_stat.st_uid != service_uid
        or status_stat.st_gid != service_gid
        or stat.S_IMODE(status_stat.st_mode) != 0o600
    ):
        raise SystemExit(1)
PY
STATUS_READY=1

for required_command in age flock gcloud mkdir sqlite3 tar; do
    command -v "$required_command" >/dev/null 2>&1 || fail
done
[[ -x "$SCRIPT_DIR/verify_backup.sh" ]] || fail

[[ -n "${AGE_RECIPIENT-}" && ${#AGE_RECIPIENT} -le 200 ]] || fail
[[ "$AGE_RECIPIENT" != *$'\n'* && "$AGE_RECIPIENT" != *$'\r'* ]] || fail
[[ "$AGE_RECIPIENT" =~ ^age1[023456789acdefghjklmnpqrstuvwxyz]{20,180}$ ]] || fail

BACKUP_BUCKET=${PERSONAL_MONITOR_BACKUP_BUCKET-}
[[ -n "$BACKUP_BUCKET" && ${#BACKUP_BUCKET} -le 227 ]] || fail
[[ "$BACKUP_BUCKET" =~ ^gs://[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$ ]] || fail
[[ "$BACKUP_BUCKET" != *".."* && "$BACKUP_BUCKET" != *".-"* ]] || fail
[[ "$BACKUP_BUCKET" != *"-."* && "$BACKUP_BUCKET" != *"_."* ]] || fail
[[ "$BACKUP_BUCKET" != *"._"* ]] || fail

if [[ ! -e "$LOCK_DIRECTORY" && ! -L "$LOCK_DIRECTORY" ]]; then
    mkdir -m 0700 -- "$LOCK_DIRECTORY" 2>/dev/null
    chown 0:0 "$LOCK_DIRECTORY" 2>/dev/null
    chmod 0700 "$LOCK_DIRECTORY" 2>/dev/null
fi
python3 - "$LOCK_DIRECTORY" "$LOCK_FILE" <<'PY' || exit 1
import os
import stat
import sys
from pathlib import Path

lock_directory = Path(sys.argv[1])
lock = Path(sys.argv[2])
try:
    directory_stat = lock_directory.lstat()
except OSError:
    raise SystemExit(1)
if (
    lock_directory.is_symlink()
    or not stat.S_ISDIR(directory_stat.st_mode)
    or directory_stat.st_uid != os.geteuid()
    or stat.S_IMODE(directory_stat.st_mode) != 0o700
    or lock.parent != lock_directory
):
    raise SystemExit(1)
flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
if hasattr(os, "O_CLOEXEC"):
    flags |= os.O_CLOEXEC
try:
    descriptor = os.open(lock, flags, 0o600)
    lock_stat = os.fstat(descriptor)
    path_stat = lock.lstat()
finally:
    try:
        os.close(descriptor)
    except (NameError, OSError):
        pass
if (
    lock.is_symlink()
    or not stat.S_ISREG(lock_stat.st_mode)
    or (lock_stat.st_dev, lock_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino)
    or lock_stat.st_uid != os.geteuid()
    or stat.S_IMODE(lock_stat.st_mode) != 0o600
):
    raise SystemExit(1)
PY
exec 9<>"$LOCK_FILE" 2>/dev/null
flock --exclusive --nonblock 9 >/dev/null 2>&1 || fail

python3 - "$DATABASE" "$ADAPTIVE" "$VAULT" "$MASTER_KEY" "$COMPOSE_CONFIG" \
    "$SERVICE_UID" "$SERVICE_GID" <<'PY' || exit 1
import os
import stat
import sys
from pathlib import Path

database, adaptive, vault, master_key, compose_config = map(Path, sys.argv[1:6])
service_uid = int(sys.argv[6])
service_gid = int(sys.argv[7])


def private_directory(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except OSError:
        raise SystemExit(1)
    if (
        path.is_symlink()
        or not stat.S_ISDIR(path_stat.st_mode)
        or path_stat.st_uid != service_uid
        or path_stat.st_gid != service_gid
        or stat.S_IMODE(path_stat.st_mode) != 0o700
    ):
        raise SystemExit(1)


def private_regular(path: Path, *, service_owned: bool = True) -> None:
    try:
        path_stat = path.lstat()
    except OSError:
        raise SystemExit(1)
    if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
        raise SystemExit(1)
    if service_owned and (
        path_stat.st_uid != service_uid
        or path_stat.st_gid != service_gid
        or stat.S_IMODE(path_stat.st_mode) != 0o600
    ):
        raise SystemExit(1)
    if not service_owned and stat.S_IMODE(path_stat.st_mode) & 0o022:
        raise SystemExit(1)


def trusted_parent(path: Path, allowed_modes: set[int]) -> None:
    try:
        path_stat = path.lstat()
    except OSError:
        raise SystemExit(1)
    owner = (path_stat.st_uid, path_stat.st_gid)
    if (
        path.is_symlink()
        or not stat.S_ISDIR(path_stat.st_mode)
        or owner not in {(0, 0), (service_uid, service_gid)}
        or stat.S_IMODE(path_stat.st_mode) not in allowed_modes
    ):
        raise SystemExit(1)


private_directory(database.parent)
private_directory(database.parents[1])
private_directory(adaptive)
private_directory(vault)
private_directory(Path("/srv/personal-monitor/logs") if str(database).startswith("/srv/") else database.parents[1] / "logs")
trusted_parent(master_key.parent, {0o700, 0o750})
trusted_parent(compose_config.parent, {0o700, 0o750, 0o755})
private_regular(database)
private_regular(master_key)
private_regular(compose_config, service_owned=False)
PY

WORKSPACE=$(mktemp -d "/tmp/personal-monitor-backup.XXXXXXXXXX" 2>/dev/null)
chmod 0700 "$WORKSPACE" 2>/dev/null
STAGING="$WORKSPACE/staging"
mkdir -m 0700 "$STAGING" 2>/dev/null
for directory in \
    data data/db data/adaptive data/vault secrets config manifest; do
    mkdir -m 0700 "$STAGING/$directory" 2>/dev/null
done

sqlite3 "$DATABASE" ".backup '$STAGING/data/db/monitor.db'" >/dev/null 2>&1
chmod 0600 "$STAGING/data/db/monitor.db" 2>/dev/null

python3 - "$ADAPTIVE" "$VAULT" "$MASTER_KEY" "$COMPOSE_CONFIG" "$STAGING" \
    "$SERVICE_UID" "$SERVICE_GID" "$ARCHIVE_TIMESTAMP" <<'PY' || exit 1
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

adaptive = Path(sys.argv[1])
vault = Path(sys.argv[2])
master_key = Path(sys.argv[3])
compose_config = Path(sys.argv[4])
staging = Path(sys.argv[5])
service_uid = int(sys.argv[6])
service_gid = int(sys.argv[7])
archive_timestamp = sys.argv[8]
MAX_MEMBERS = 10_000
MAX_MEMBER_SIZE = 128 * 1024 * 1024
MAX_TOTAL_SIZE = 512 * 1024 * 1024
MAX_PATH_BYTES = 512
member_count = 0
total_size = 0


def checked_name(name: str) -> None:
    if (
        name in {"", ".", ".."}
        or "/" in name
        or len(name.encode("utf-8")) > MAX_PATH_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise SystemExit(1)


def copy_open_file(source_fd: int, destination: Path, expected_stat: os.stat_result) -> None:
    global member_count, total_size
    opened_stat = os.fstat(source_fd)
    if (
        not stat.S_ISREG(opened_stat.st_mode)
        or (opened_stat.st_dev, opened_stat.st_ino)
        != (expected_stat.st_dev, expected_stat.st_ino)
        or opened_stat.st_uid != service_uid
        or opened_stat.st_gid != service_gid
        or stat.S_IMODE(opened_stat.st_mode) != 0o600
        or opened_stat.st_size > MAX_MEMBER_SIZE
    ):
        raise SystemExit(1)
    member_count += 1
    total_size += opened_stat.st_size
    if member_count > MAX_MEMBERS or total_size > MAX_TOTAL_SIZE:
        raise SystemExit(1)
    with os.fdopen(os.dup(source_fd), "rb") as source:
        with destination.open("xb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
    destination.chmod(0o600)


def copy_tree_fd(source_fd: int, destination: Path) -> None:
    source_stat = os.fstat(source_fd)
    if (
        not stat.S_ISDIR(source_stat.st_mode)
        or source_stat.st_uid != service_uid
        or source_stat.st_gid != service_gid
        or stat.S_IMODE(source_stat.st_mode) != 0o700
    ):
        raise SystemExit(1)
    for name in sorted(os.listdir(source_fd)):
        checked_name(name)
        entry_stat = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        destination_entry = destination / name
        if stat.S_ISDIR(entry_stat.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=source_fd,
            )
            try:
                destination_entry.mkdir(mode=0o700)
                copy_tree_fd(child_fd, destination_entry)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(entry_stat.st_mode):
            child_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd)
            try:
                copy_open_file(child_fd, destination_entry, entry_stat)
            finally:
                os.close(child_fd)
        else:
            raise SystemExit(1)


def copy_tree(source: Path, destination: Path) -> None:
    descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        copy_tree_fd(descriptor, destination)
    finally:
        os.close(descriptor)


def copy_fixed_file(source: Path, destination: Path, *, service_owned: bool) -> None:
    parent_descriptor = os.open(
        source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        parent_stat = os.fstat(parent_descriptor)
        owner = (parent_stat.st_uid, parent_stat.st_gid)
        allowed_modes = {0o700, 0o750} if service_owned else {0o700, 0o750, 0o755}
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or owner not in {(0, 0), (service_uid, service_gid)}
            or stat.S_IMODE(parent_stat.st_mode) not in allowed_modes
        ):
            raise SystemExit(1)
        source_stat = os.stat(
            source.name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        descriptor = os.open(
            source.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
    except BaseException:
        os.close(parent_descriptor)
        raise
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise SystemExit(1)
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            source_stat.st_dev,
            source_stat.st_ino,
        ):
            raise SystemExit(1)
        if service_owned and (
            opened_stat.st_uid != service_uid
            or opened_stat.st_gid != service_gid
            or stat.S_IMODE(opened_stat.st_mode) != 0o600
        ):
            raise SystemExit(1)
        if not service_owned and stat.S_IMODE(opened_stat.st_mode) & 0o022:
            raise SystemExit(1)
        with os.fdopen(os.dup(descriptor), "rb") as input_file:
            with destination.open("xb") as output:
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
        destination.chmod(0o600)
    finally:
        os.close(descriptor)
        os.close(parent_descriptor)


copy_tree(adaptive, staging / "data" / "adaptive")
copy_tree(vault, staging / "data" / "vault")
copy_fixed_file(master_key, staging / "secrets" / "master.key", service_owned=True)
copy_fixed_file(
    compose_config, staging / "config" / "compose.yaml", service_owned=False
)

metadata = {"archive_timestamp": archive_timestamp, "schema_version": 1}
metadata_path = staging / "manifest" / "backup.json"
metadata_path.write_text(
    json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
metadata_path.chmod(0o600)

allowed_fixed_files = {
    "data/db/monitor.db",
    "secrets/master.key",
    "config/compose.yaml",
    "manifest/backup.json",
}
allowed_directories = {
    "data",
    "data/db",
    "data/adaptive",
    "data/vault",
    "secrets",
    "config",
    "manifest",
}
regular_files: list[tuple[str, Path]] = []
for path in staging.rglob("*"):
    relative = path.relative_to(staging).as_posix()
    path_stat = path.lstat()
    if path.is_symlink():
        raise SystemExit(1)
    if stat.S_ISDIR(path_stat.st_mode):
        if relative not in allowed_directories and not relative.startswith(
            ("data/adaptive/", "data/vault/")
        ):
            raise SystemExit(1)
    elif stat.S_ISREG(path_stat.st_mode):
        if relative not in allowed_fixed_files and not relative.startswith(
            ("data/adaptive/", "data/vault/")
        ):
            raise SystemExit(1)
        regular_files.append((relative, path))
    else:
        raise SystemExit(1)

# Closed manifest path: manifest/SHA256SUMS
manifest_path = staging / "manifest" / "SHA256SUMS"
with manifest_path.open("x", encoding="utf-8", newline="\n") as manifest:
    for relative, path in sorted(regular_files):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest.write(f"{digest}  {relative}\n")
manifest_path.chmod(0o600)
PY

tar_stream() {
    tar \
        --sort=name \
        --mtime=@0 \
        --owner=0 \
        --group=0 \
        --numeric-owner \
        --format=gnu \
        -C "$STAGING" \
        config data manifest secrets \
        2>/dev/null
}

PLAINTEXT_TAR="$WORKSPACE/plaintext.tar"
tar_stream >"$PLAINTEXT_TAR"
chmod 0600 "$PLAINTEXT_TAR" 2>/dev/null
SMOKE_TARGET=$(mktemp -d "$WORKSPACE/smoke.XXXXXXXXXX" 2>/dev/null)
chmod 0700 "$SMOKE_TARGET" 2>/dev/null
"$SCRIPT_DIR/verify_backup.sh" "$PLAINTEXT_TAR" "$SMOKE_TARGET" \
    >/dev/null 2>&1 || fail
rm -rf -- "$SMOKE_TARGET" >/dev/null 2>&1

ENCRYPTED_ARCHIVE="$WORKSPACE/${ARCHIVE_TIMESTAMP}.tar.age"
tar_stream | age --recipient "$AGE_RECIPIENT" --output "$ENCRYPTED_ARCHIVE" \
    2>/dev/null
chmod 0600 "$ENCRYPTED_ARCHIVE" 2>/dev/null

LOCAL_METADATA=$(python3 - "$ENCRYPTED_ARCHIVE" <<'PY'
import hashlib
import sys
from pathlib import Path

archive = Path(sys.argv[1])
digest = hashlib.sha256()
size = 0
with archive.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
print(f"{digest.hexdigest()}:{size}")
PY
)
LOCAL_DIGEST=${LOCAL_METADATA%%:*}
LOCAL_SIZE=${LOCAL_METADATA#*:}
[[ "$LOCAL_DIGEST" =~ ^[0-9a-f]{64}$ && "$LOCAL_SIZE" =~ ^[0-9]+$ ]] || fail

upload_and_verify() {
    local object_uri=$1
    local description
    gcloud storage cp \
        "$ENCRYPTED_ARCHIVE" \
        "$object_uri" \
        --if-generation-match=0 \
        --content-type=application/octet-stream \
        --custom-metadata="sha256=$LOCAL_DIGEST,size=$LOCAL_SIZE" \
        >/dev/null 2>&1
    description=$(mktemp "$WORKSPACE/description.XXXXXXXXXX")
    gcloud storage objects describe "$object_uri" \
        --raw \
        '--format=json(size,metadata)' >"$description" 2>/dev/null
    python3 - "$description" "$LOCAL_DIGEST" "$LOCAL_SIZE" <<'PY' || exit 1
import json
import sys
from pathlib import Path


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


try:
    value = json.loads(
        Path(sys.argv[1]).read_text(encoding="utf-8"),
        object_pairs_hook=unique_object,
    )
except (OSError, UnicodeError, ValueError):
    raise SystemExit(1)
if type(value) is not dict or set(value) != {"metadata", "size"}:
    raise SystemExit(1)
metadata = value["metadata"]
if type(metadata) is not dict:
    raise SystemExit(1)
if (
    str(value["size"]) != sys.argv[3]
    or metadata.get("sha256") != sys.argv[2]
    or metadata.get("size") != sys.argv[3]
):
    raise SystemExit(1)
PY
}

DAILY_OBJECT="$BACKUP_BUCKET/daily/${ARCHIVE_TIMESTAMP}.tar.age"
upload_and_verify "$DAILY_OBJECT"
if [[ "$ARCHIVE_WEEKDAY" == 7 ]]; then
    WEEKLY_OBJECT="$BACKUP_BUCKET/weekly/${ARCHIVE_TIMESTAMP}.tar.age"
    upload_and_verify "$WEEKLY_OBJECT"
fi

validate_retention_listing() {
    local prefix=$1
    local keep=$2
    local listing=$3
    local deletion_plan=$4
    python3 - "$prefix" "$keep" "$listing" "$deletion_plan" <<'PY' || exit 1
import json
import datetime as dt
import re
import sys
from pathlib import Path


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


prefix = sys.argv[1]
keep = int(sys.argv[2])
listing_path = Path(sys.argv[3])
deletion_path = Path(sys.argv[4])
pattern = re.compile(
    rf"\A{re.escape(prefix)}/"
    r"(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-"
    r"(?:0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3])"
    r"[0-5]\d[0-5]\dZ\.tar\.age\Z"
)
try:
    listing = json.loads(
        listing_path.read_text(encoding="utf-8"),
        object_pairs_hook=unique_object,
    )
except (OSError, UnicodeError, ValueError):
    raise SystemExit(1)
if type(listing) is not list or len(listing) > 10_000:
    raise SystemExit(1)
objects: list[tuple[str, str]] = []
for entry in listing:
    if type(entry) is not dict or set(entry) != {"generation", "name"}:
        raise SystemExit(1)
    name = entry["name"]
    if type(name) is not str or pattern.fullmatch(name) is None:
        raise SystemExit(1)
    timestamp = name.removeprefix(f"{prefix}/").removesuffix(".tar.age")
    try:
        parsed = dt.datetime.strptime(timestamp, "%Y-%m-%dT%H%M%SZ")
    except ValueError:
        raise SystemExit(1)
    if parsed.strftime("%Y-%m-%dT%H%M%SZ") != timestamp:
        raise SystemExit(1)
    generation_value = entry["generation"]
    if type(generation_value) not in {int, str}:
        raise SystemExit(1)
    generation = str(generation_value)
    if not generation.isascii() or not generation.isdigit() or int(generation) <= 0:
        raise SystemExit(1)
    objects.append((name, generation))
names = [name for name, _ in objects]
if len(names) != len(set(names)):
    raise SystemExit(1)
with deletion_path.open("x", encoding="utf-8", newline="\n") as output:
    for name, generation in sorted(objects, reverse=True)[keep:]:
        output.write(f"{name}\t{generation}\n")
PY
}

DAILY_LISTING="$WORKSPACE/daily-listing.json"
WEEKLY_LISTING="$WORKSPACE/weekly-listing.json"
DAILY_DELETIONS="$WORKSPACE/daily-deletions"
WEEKLY_DELETIONS="$WORKSPACE/weekly-deletions"
gcloud storage objects list "$BACKUP_BUCKET/daily/" \
    '--format=json(name,generation)' >"$DAILY_LISTING" 2>/dev/null
gcloud storage objects list "$BACKUP_BUCKET/weekly/" \
    '--format=json(name,generation)' >"$WEEKLY_LISTING" 2>/dev/null
validate_retention_listing "daily" "7" "$DAILY_LISTING" "$DAILY_DELETIONS"
validate_retention_listing "weekly" "4" "$WEEKLY_LISTING" "$WEEKLY_DELETIONS"

while IFS=$'\t' read -r object_name object_generation; do
    [[ -n "$object_name" && "$object_generation" =~ ^[0-9]+$ ]] || fail
    gcloud storage rm "$BACKUP_BUCKET/$object_name" \
        --if-generation-match="$object_generation" >/dev/null 2>&1
done <"$DAILY_DELETIONS"
while IFS=$'\t' read -r object_name object_generation; do
    [[ -n "$object_name" && "$object_generation" =~ ^[0-9]+$ ]] || fail
    gcloud storage rm "$BACKUP_BUCKET/$object_name" \
        --if-generation-match="$object_generation" >/dev/null 2>&1
done <"$WEEKLY_DELETIONS"

COMPLETE=1
