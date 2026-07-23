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
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self._print_message("invalid command arguments\n", sys.stderr)
        raise SystemExit(2)


def open_database(path: Path):
    from personal_monitor.storage import open_database as implementation

    return implementation(path)


def open_existing_database(path: Path):
    from personal_monitor.storage import open_existing_database as implementation

    return implementation(path)


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
    sub.add_parser("serve")
    ai_worker = sub.add_parser("ai-worker")
    ai_worker.add_argument("--socket", type=Path, required=True)
    run_once = sub.add_parser("run-once")
    run_once.add_argument("--database", type=Path, required=True)
    run_once.add_argument("--monitor", required=True)
    run_once.add_argument(
        "--delivery",
        choices=("enabled", "disabled"),
        default="disabled",
    )
    migration = sub.add_parser("migration")
    migration_subcommands = migration.add_subparsers(dest="migration_action", required=True)
    import_rental = migration_subcommands.add_parser("import-rental")
    import_rental.add_argument("--source", type=Path, required=True)
    import_rental.add_argument("--database", type=Path, required=True)
    import_rental.add_argument("--owner", required=True)
    import_rental.add_argument("--target", required=True)
    import_rental.add_argument("--dry-run", action="store_true")
    shadow_run = migration_subcommands.add_parser("shadow-run")
    shadow_run.add_argument("--source", type=Path, required=True)
    shadow_run.add_argument("--database", type=Path, required=True)
    shadow_run.add_argument("--run-date", required=True)
    duplicate_probe = migration_subcommands.add_parser("duplicate-probe")
    duplicate_probe.add_argument("--database", type=Path, required=True)
    duplicate_probe.add_argument("--monitor", required=True)
    migration_status = migration_subcommands.add_parser("status")
    migration_status.add_argument("--database", type=Path, required=True)
    migration_status.add_argument("--as-of")
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
    if arguments.command == "serve":
        try:
            return _serve_command()
        except (OSError, RuntimeError, sqlite3.Error, ValueError):
            print("personal monitor service failed", file=sys.stderr)
            return 1
    if arguments.command == "ai-worker":
        if _ai_worker_environment_refused(os.environ):
            print("ai worker configuration refused", file=sys.stderr)
            return 1
        try:
            return _ai_worker_command(arguments.socket)
        except (OSError, RuntimeError, sqlite3.Error, ValueError):
            print("ai worker failed", file=sys.stderr)
            return 1
    if arguments.command == "run-once":
        try:
            return _run_once_command(
                arguments.database,
                arguments.monitor,
                arguments.delivery,
            )
        except (OSError, RuntimeError, sqlite3.Error, ValueError):
            print("run-once failed", file=sys.stderr)
            return 1
    if arguments.command == "migration":
        if arguments.migration_action == "import-rental":
            return _migration_import_rental(
                arguments.source,
                arguments.database,
                arguments.owner,
                arguments.target,
                dry_run=arguments.dry_run,
            )
        if arguments.migration_action == "shadow-run":
            return _migration_shadow_run(
                arguments.source,
                arguments.database,
                arguments.run_date,
            )
        if arguments.migration_action == "duplicate-probe":
            return _migration_duplicate_probe(
                arguments.database,
                arguments.monitor,
            )
        return _migration_status(arguments.database, arguments.as_of)
    return 2


def _validate_spec(path: Path) -> int:
    from personal_monitor.domain.spec import MonitorSpec
    from personal_monitor.storage.schema import canonical_json

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
    from personal_monitor.maintenance import Maintenance
    from personal_monitor.storage.schema import utc_now

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


def _ai_worker_environment_refused(environment: object) -> bool:
    try:
        items = environment.items()
    except Exception:
        return True
    for name, value in items:
        if type(name) is not str or type(value) is not str:
            return True
        if value == "":
            continue
        if (
            name in {"DATABASE_PATH", "OPENAI_API_KEY", "CODEX_API_KEY"}
            or name.startswith("TELEGRAM_")
            or name.startswith("PERSONAL_MONITOR_TELEGRAM_")
            or name == "PERSONAL_MONITOR_DATABASE_PATH"
        ):
            return True
    return False


def _ai_worker_command(socket_path: Path) -> int:
    return asyncio.run(_run_ai_worker(Path(socket_path)))


async def _run_ai_worker(socket_path: Path) -> int:
    import signal

    from personal_monitor.ai.auth import CodexAuthGuard
    from personal_monitor.ai.codex_cli import CodexCli
    from personal_monitor.ai.worker import CodexWorkerServer

    codex_home = Path(
        os.environ.get("PERSONAL_MONITOR_CODEX_HOME", "/srv/personal-monitor/codex-home")
    )
    task_root = Path(
        os.environ.get("PERSONAL_MONITOR_CODEX_TASK_ROOT", "/run/personal-monitor-codex")
    )
    _ensure_private_directory(codex_home)
    _ensure_private_directory(task_root)
    _ensure_private_directory(socket_path.parent)
    codex_binary = os.environ.get("PERSONAL_MONITOR_CODEX_BINARY", "codex")
    node_binary = os.environ.get("PERSONAL_MONITOR_NODE_BINARY") or None
    guard = CodexAuthGuard(
        codex_binary,
        codex_home,
        node_binary=node_binary,
    )
    cli = CodexCli(
        codex_binary,
        codex_home,
        task_root,
        auth_guard=guard,
        node_binary=node_binary,
    )
    server = CodexWorkerServer(
        socket_path,
        cli,
        auth_check=guard.check,
    )
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[tuple[signal.Signals, object]] = []
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                previous = signal.getsignal(signum)
                loop.add_signal_handler(signum, stopped.set)
                installed.append((signum, previous))
            except (NotImplementedError, RuntimeError, ValueError):
                continue
        await server.start()
        await stopped.wait()
        return 0
    finally:
        for signum, previous in installed:
            with suppress(Exception):
                loop.remove_signal_handler(signum)
                signal.signal(signum, previous)
        await asyncio.shield(server.close())


def _ensure_private_directory(path: Path) -> None:
    raw_path = os.fspath(path)
    if (
        not path.is_absolute()
        or path != Path(os.path.normpath(path))
        or raw_path != os.path.normpath(raw_path)
    ):
        raise ValueError("invalid private directory")
    with suppress(FileExistsError):
        os.mkdir(path, 0o700)
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("invalid private directory")


def _serve_command() -> int:
    from personal_monitor.config import Settings
    from personal_monitor.service import build_service

    settings = Settings.from_env()
    return asyncio.run(_run_service(build_service(settings)))


async def _run_service(service: object) -> int:
    run = getattr(service, "run", None)
    if not callable(run):
        raise RuntimeError("invalid service")
    await run()
    return 0


def _run_once_command(database: Path, monitor: str, delivery: str) -> int:
    from personal_monitor.service import run_monitor_once

    if delivery not in {"enabled", "disabled"}:
        raise ValueError("invalid delivery mode")
    return asyncio.run(
        run_monitor_once(
            database_path=Path(database),
            monitor_id=monitor,
            delivery_enabled=delivery == "enabled",
        )
    )


def _migration_import_rental(
    source: Path,
    database: Path,
    owner: str,
    target: str,
    *,
    dry_run: bool,
) -> int:
    from personal_monitor.migration import import_rental_state
    from personal_monitor.storage.schema import canonical_json

    try:
        report = import_rental_state(
            source,
            database,
            owner,
            target,
            dry_run=dry_run,
        )
    except Exception:
        print("rental import failed", file=sys.stderr)
        return 1
    print(canonical_json(asdict(report)))
    return 0


def _migration_shadow_run(source: Path, database: Path, run_date: str) -> int:
    from personal_monitor.adapters.rental_housing import production_rental_housing_adapter
    from personal_monitor.migration.import_rental import _normalized_absolute_path
    from personal_monitor.migration.shadow import ShadowRepository, run_shadow_fetch
    from personal_monitor.service import NullDeliverySender
    from personal_monitor.storage.schema import canonical_json

    connection: sqlite3.Connection | None = None
    try:
        key = os.environ.get("PERSONAL_MONITOR_DATA_GO_KR_SERVICE_KEY")
        if type(key) is not str or not key:
            raise ValueError("data key is unavailable")
        target = _normalized_absolute_path(database)
        requested = _canonical_cli_date(run_date)
        connection = open_existing_database(target)
        result = asyncio.run(
            run_shadow_fetch(
                source,
                ShadowRepository(connection),
                requested,
                adapter=production_rental_housing_adapter(key),
                sender=NullDeliverySender(),
            )
        )
        print(canonical_json(_shadow_result_output(result)))
        return 0
    except Exception:
        print("rental shadow failed", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


def _migration_duplicate_probe(database: Path, monitor: str) -> int:
    from personal_monitor.adapters.rental_housing import production_rental_housing_adapter
    from personal_monitor.migration.import_rental import _normalized_absolute_path
    from personal_monitor.migration.shadow import ShadowRepository, run_duplicate_probe
    from personal_monitor.service import NullDeliverySender
    from personal_monitor.storage.schema import canonical_json

    connection: sqlite3.Connection | None = None
    try:
        key = os.environ.get("PERSONAL_MONITOR_DATA_GO_KR_SERVICE_KEY")
        if type(key) is not str or not key:
            raise ValueError("data key is unavailable")
        target = _normalized_absolute_path(database)
        connection = open_existing_database(target)
        result = asyncio.run(
            run_duplicate_probe(
                ShadowRepository(connection),
                monitor,
                adapter=production_rental_housing_adapter(key),
                sender=NullDeliverySender(),
            )
        )
        print(
            canonical_json(
                {
                    "monitor_id": result.monitor_id,
                    "run_date": result.run_date.isoformat(),
                    "current_hash": result.current_hash,
                    "passed": result.passed,
                    "missing_ids": result.missing_ids,
                    "conflicting_ids": result.conflicting_ids,
                }
            )
        )
        return 0
    except Exception:
        print("rental duplicate probe failed", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


def _migration_status(database: Path, as_of: str | None) -> int:
    from personal_monitor.migration.import_rental import _normalized_absolute_path
    from personal_monitor.migration.shadow import ShadowRepository
    from personal_monitor.storage.schema import canonical_json

    connection: sqlite3.Connection | None = None
    try:
        requested = (
            datetime.now(ZoneInfo("Asia/Seoul")).date()
            if as_of is None
            else _canonical_cli_date(as_of)
        )
        target = _normalized_absolute_path(database)
        connection = open_existing_database(target)
        status = ShadowRepository(connection).status(requested)
        print(
            canonical_json(
                {
                    "consecutive_matches": status.consecutive_matches,
                    "last_match_date": (
                        status.last_match_date.isoformat()
                        if status.last_match_date is not None
                        else None
                    ),
                    "unresolved_differences": status.unresolved_differences,
                    "state_imported": status.state_imported,
                    "duplicate_probe_passed": status.duplicate_probe_passed,
                    "cutover_ready": status.cutover_ready,
                }
            )
        )
        return 0
    except Exception:
        print("rental migration status failed", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


def _canonical_cli_date(value: object) -> date:
    if type(value) is not str or len(value) != 10:
        raise ValueError("date is invalid")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date is invalid")
    return parsed


def _shadow_result_output(result: object) -> dict[str, object]:
    from personal_monitor.migration.shadow import ShadowResult

    if type(result) is not ShadowResult:
        raise ValueError("shadow result is invalid")
    return {
        "run_date": result.run_date.isoformat(),
        "old_hash": result.old_hash,
        "new_hash": result.new_hash,
        "matched": result.matched,
        "differences": tuple(
            {
                "agency": difference.agency,
                "missing_ids": difference.missing_ids,
                "extra_ids": difference.extra_ids,
            }
            for difference in result.differences
        ),
        "old_status": result.old_status,
        "new_status": result.new_status,
    }
