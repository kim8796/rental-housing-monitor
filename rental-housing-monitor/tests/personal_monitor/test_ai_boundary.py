from __future__ import annotations

import ast
import inspect
from pathlib import Path

from personal_monitor import ports
from personal_monitor.engine import outbox, runner, scheduler
from personal_monitor.engine.runner import MonitorRunner


def test_scheduled_runtime_import_graph_contains_no_ai_module() -> None:
    pending = [Path(module.__file__) for module in (ports, scheduler, runner, outbox)]
    visited: set[Path] = set()
    package_root = Path(ports.__file__).parent

    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not any(
            name == "personal_monitor.ai" or name.startswith("personal_monitor.ai.")
            for name in imported_modules
        )
        assert "personal_monitor.ai" not in source
        for name in imported_modules:
            if not name.startswith("personal_monitor."):
                continue
            relative = name.removeprefix("personal_monitor.").replace(".", "/")
            module_path = package_root / f"{relative}.py"
            package_path = package_root / relative / "__init__.py"
            if module_path.exists():
                pending.append(module_path)
            elif package_path.exists():
                pending.append(package_path)


def test_runner_only_queues_and_has_no_delivery_sender_dependency() -> None:
    parameters = inspect.signature(MonitorRunner).parameters

    assert "sender" not in parameters
    assert "DeliverySender" not in inspect.getsource(runner)
    assert ".send(" not in inspect.getsource(runner)
