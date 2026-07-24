#!/usr/bin/env bash
set -Eeuo pipefail
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077

fail() {
    printf '%s\n' "personal monitor restore failed" >&2
    exit 1
}

fail_nonempty() {
    printf '%s\n' "TARGET_MUST_BE_EMPTY" >&2
    exit 1
}

if (( $# != 3 )); then
    fail
fi

ARCHIVE=$1
TARGET=$2
IDENTITY=$3
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
WORKSPACE=
VERIFY_DIR=
MOVE_STARTED=0
COMPLETE=0
TARGET_DEVICE=
TARGET_INODE=

for required_command in age cat chmod dirname find mkdir mktemp mv python3 rm; do
    command -v "$required_command" >/dev/null 2>&1 || fail
done
[[ -x "$SCRIPT_DIR/verify_backup.sh" ]] || fail

validate_inputs() {
    local validation
    if validation=$(python3 - "$ARCHIVE" "$TARGET" "$IDENTITY" <<'PY'
import os
import stat
import sys
from pathlib import Path

archive = Path(sys.argv[1])
target = Path(sys.argv[2])
identity = Path(sys.argv[3])

try:
    archive_stat = archive.lstat()
    target_stat = target.lstat()
    identity_stat = identity.lstat()
except OSError:
    raise SystemExit(1)

if (
    stat.S_ISLNK(archive_stat.st_mode)
    or not stat.S_ISREG(archive_stat.st_mode)
    or archive_stat.st_size <= 0
):
    raise SystemExit(1)
if (
    stat.S_ISLNK(identity_stat.st_mode)
    or not stat.S_ISREG(identity_stat.st_mode)
    or identity_stat.st_uid != os.geteuid()
    or stat.S_IMODE(identity_stat.st_mode) & 0o077
):
    raise SystemExit(1)
if (
    stat.S_ISLNK(target_stat.st_mode)
    or not stat.S_ISDIR(target_stat.st_mode)
    or target_stat.st_uid != os.geteuid()
    or stat.S_IMODE(target_stat.st_mode) != 0o700
):
    raise SystemExit(1)
if any(target.iterdir()):
    raise SystemExit(2)
print(f"{target_stat.st_dev}:{target_stat.st_ino}")
PY
    ); then
        :
    else
        local status=$?
        if (( status == 2 )); then
            fail_nonempty
        fi
        fail
    fi
    TARGET_DEVICE=${validation%%:*}
    TARGET_INODE=${validation#*:}
    [[ "$TARGET_DEVICE" =~ ^[0-9]+$ && "$TARGET_INODE" =~ ^[0-9]+$ ]] || fail
}

target_is_still_dedicated() {
    [[ -d "$TARGET" && ! -L "$TARGET" ]] || return 1
    python3 - "$TARGET" "$TARGET_DEVICE" "$TARGET_INODE" <<'PY' >/dev/null 2>&1
import os
import stat
import sys
from pathlib import Path

target = Path(sys.argv[1])
try:
    target_stat = target.lstat()
except OSError:
    raise SystemExit(1)
if (
    stat.S_ISLNK(target_stat.st_mode)
    or not stat.S_ISDIR(target_stat.st_mode)
    or str(target_stat.st_dev) != sys.argv[2]
    or str(target_stat.st_ino) != sys.argv[3]
    or target_stat.st_uid != os.geteuid()
    or stat.S_IMODE(target_stat.st_mode) != 0o700
):
    raise SystemExit(1)
PY
}

remove_target_children() {
    target_is_still_dedicated || return 1
    find -P "$TARGET" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
}

cleanup() {
    local exit_status=$?
    trap - EXIT
    if (( MOVE_STARTED == 1 && COMPLETE == 0 )); then
        remove_target_children >/dev/null 2>&1 || true
    fi
    if [[ -n "$WORKSPACE" && -d "$WORKSPACE" && ! -L "$WORKSPACE" ]]; then
        if ! rm -rf -- "$WORKSPACE" >/dev/null 2>&1; then
            exit_status=1
        fi
    fi
    exit "$exit_status"
}
trap cleanup EXIT

validate_inputs

TARGET_PARENT=$(dirname -- "$TARGET")
[[ -d "$TARGET_PARENT" && ! -L "$TARGET_PARENT" ]] || fail
WORKSPACE=$(mktemp -d "$TARGET_PARENT/.personal-monitor-restore.XXXXXXXXXX" 2>/dev/null)
chmod 0700 "$WORKSPACE" 2>/dev/null
DECRYPTED=$(mktemp "$WORKSPACE/plaintext.XXXXXXXXXX" 2>/dev/null)
chmod 0600 "$DECRYPTED" 2>/dev/null
VERIFY_DIR="$WORKSPACE/verified"
mkdir -m 0700 "$VERIFY_DIR" 2>/dev/null
VERIFY_OUTPUT="$WORKSPACE/verify-output"

age --decrypt --identity "$IDENTITY" --output "$DECRYPTED" "$ARCHIVE" \
    >/dev/null 2>&1 || fail
if ! "$SCRIPT_DIR/verify_backup.sh" "$DECRYPTED" "$VERIFY_DIR" \
    >"$VERIFY_OUTPUT" 2>/dev/null; then
    fail
fi
chmod 0600 "$VERIFY_OUTPUT" 2>/dev/null

target_is_still_dedicated || fail
if [[ -n "$(find -P "$TARGET" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    fail_nonempty
fi

MOVE_STARTED=1
for child in data secrets config manifest; do
    mv -- "$VERIFY_DIR/$child" "$TARGET/" 2>/dev/null
done

python3 - "$TARGET" <<'PY' || exit 1
import os
import stat
import sys
from pathlib import Path

target = Path(sys.argv[1])
for path in target.rglob("*"):
    path_stat = path.lstat()
    if stat.S_ISLNK(path_stat.st_mode):
        raise SystemExit(1)
    if not (stat.S_ISDIR(path_stat.st_mode) or stat.S_ISREG(path_stat.st_mode)):
        raise SystemExit(1)
PY
find -P "$TARGET" -type d -exec chmod 0700 {} + 2>/dev/null
find -P "$TARGET" -type f -exec chmod 0600 {} + 2>/dev/null

cat "$VERIFY_OUTPUT" 2>/dev/null
COMPLETE=1
