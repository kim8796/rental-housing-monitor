from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sqlite3
import stat
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from personal_monitor.domain.spec import MonitorSpec
from personal_monitor.maintenance import Maintenance
from personal_monitor.storage import open_database, open_existing_database
from personal_monitor.storage.schema import canonical_json, utc_now


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self._print_message("invalid command arguments\n", sys.stderr)
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="personal-monitor")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-spec")
    validate.add_argument("path", type=Path)
    database = sub.add_parser("database")
    database.add_argument("action", choices=("init", "integrity-check", "vacuum"))
    database.add_argument("--path", type=Path, required=True)
    maintenance = sub.add_parser("maintenance")
    maintenance.add_argument("action", choices=("run",))
    maintenance.add_argument("--database", type=Path, required=True)
    profile = sub.add_parser("profile")
    profile.add_argument("action", choices=("bootstrap",))
    profile.add_argument("--id", dest="profile_id", required=True)
    profile.add_argument("--url", required=True)
    profile.add_argument("--profiles-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    if arguments.command == "validate-spec":
        return _validate_spec(arguments.path)
    if arguments.command == "database":
        return _database(arguments.action, arguments.path)
    if arguments.command == "maintenance":
        return _maintenance(arguments.database)
    if arguments.command == "profile":
        try:
            _profile_bootstrap_command(
                arguments.profile_id,
                arguments.url,
                arguments.profiles_root,
            )
        except BaseException:
            print("profile bootstrap failed", file=sys.stderr)
            return 1
        return 0
    return 2


def _validate_spec(path: Path) -> int:
    try:
        spec = MonitorSpec.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("invalid monitor specification", file=sys.stderr)
        return 2
    print(canonical_json(spec.model_dump(mode="json")))
    return 0


def _database(action: str, path: Path) -> int:
    connection: sqlite3.Connection | None = None
    try:
        if action == "init":
            connection = open_database(path)
            return 0
        connection = open_existing_database(path)
        if action == "integrity-check":
            rows = list(connection.execute("PRAGMA integrity_check"))
            if len(rows) == 1 and rows[0][0] == "ok":
                return 0
            print("database integrity check failed", file=sys.stderr)
            return 1
        connection.execute("VACUUM")
        return 0
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        print("database command failed", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


def _maintenance(path: Path) -> int:
    connection: sqlite3.Connection | None = None
    try:
        connection = open_existing_database(path)
        Maintenance(connection).run(now=utc_now())
        return 0
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        print("maintenance command failed", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


class _SystemResolver:
    async def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        return tuple(record[4][0] for record in records)


def _profile_bootstrap_command(profile_id: str, url: str, profiles_root: Path) -> None:
    from scrapling.fetchers import DynamicFetcher

    from personal_monitor.scraping.profiles import BrowserProfileStore, bootstrap_profile
    from personal_monitor.scraping.scrapling_backend import _invoke_quietly
    from personal_monitor.security.url_policy import UrlPolicy
    from personal_monitor.security.vault import CredentialVault, validate_logical_key

    validate_logical_key(profile_id)
    target = asyncio.run(UrlPolicy(_SystemResolver()).validate(url))
    root = Path(profiles_root)
    with suppress(FileExistsError):
        os.mkdir(root, 0o700)
    metadata = os.stat(root, follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RuntimeError("untrusted profile root")

    vault = CredentialVault(root / "vault", key_path=root / "master.key")
    workspace_root = Path(
        os.environ.get("PERSONAL_MONITOR_PROFILE_TMPFS_ROOT", "/dev/shm/personal-monitor")
    )
    store = BrowserProfileStore(
        vault,
        materialization_root=workspace_root,
        require_memory_backed=True,
    )

    def runner(target_url: str, **kwargs: object) -> object:
        return _invoke_quietly(DynamicFetcher.fetch, target_url, kwargs)

    def wait_for_operator(_page: object) -> None:
        input("Complete login in the browser, then press Enter to continue: ")

    bootstrap_profile(
        store,
        profile_id,
        target,
        runner=runner,
        egress_proxy_url=os.environ.get("PERSONAL_MONITOR_EGRESS_PROXY_URL", ""),
        page_action=wait_for_operator,
    )
