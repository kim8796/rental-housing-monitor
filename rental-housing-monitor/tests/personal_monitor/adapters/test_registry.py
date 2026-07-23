import builtins
import importlib

import pytest

from personal_monitor.adapters.registry import DefaultAdapterRegistry
from personal_monitor.domain.spec import SourceAdapterKind
from personal_monitor.engine.errors import ErrorClass, MonitorError


def test_registry_resolves_only_exact_builtin_keys() -> None:
    scrapling = object()
    official = object()
    registry = DefaultAdapterRegistry(scrapling=scrapling, official_api=official)

    assert registry.resolve(SourceAdapterKind.SCRAPLING, None) is scrapling
    assert registry.resolve(SourceAdapterKind.OFFICIAL_API, "json_get") is official

    for kind, ref in (
        (SourceAdapterKind.SCRAPLING, "json_get"),
        (SourceAdapterKind.OFFICIAL_API, None),
        (SourceAdapterKind.OFFICIAL_API, "other"),
        (SourceAdapterKind.PYTHON_PLUGIN, None),
    ):
        with pytest.raises(MonitorError) as caught:
            registry.resolve(kind, ref)
        assert caught.value.error_class is ErrorClass.POLICY


def test_registry_resolves_only_literal_rental_plugin_without_dynamic_fallback() -> None:
    scrapling = object()
    official = object()
    plugin = object()
    plugins = {"rental_housing": plugin, "approved": object()}
    registry = DefaultAdapterRegistry(
        scrapling=scrapling,
        official_api=official,
        python_plugins=plugins,
    )
    plugins["rental_housing"] = object()
    plugins["later"] = object()

    assert registry.resolve(SourceAdapterKind.PYTHON_PLUGIN, "rental_housing") is plugin
    with pytest.raises(MonitorError):
        registry.resolve(SourceAdapterKind.PYTHON_PLUGIN, "approved")
    with pytest.raises(MonitorError):
        registry.resolve(SourceAdapterKind.PYTHON_PLUGIN, "later")
    with pytest.raises(MonitorError) as caught:
        registry.resolve(SourceAdapterKind.PYTHON_PLUGIN, "secret.module:factory")

    assert "secret.module" not in str(caught.value)
    assert "secret.module" not in repr(caught.value)


@pytest.mark.parametrize(
    "reference",
    [
        "",
        " ",
        "rental_housing ",
        "Rental_housing",
        "rental.housing",
        "rental:housing",
        "rental_housіng",  # Cyrillic small letter Byelorussian-Ukrainian i.
        "__import__('os')",
    ],
)
def test_registry_rejects_non_literal_plugin_references(reference: str) -> None:
    registry = DefaultAdapterRegistry(
        scrapling=object(),
        official_api=object(),
        rental_housing=object(),
    )

    with pytest.raises(MonitorError) as caught:
        registry.resolve(SourceAdapterKind.PYTHON_PLUGIN, reference)

    assert caught.value.error_class is ErrorClass.POLICY
    if len(reference.strip()) > 1:
        assert reference not in str(caught.value)
        assert reference not in repr(caught.value)


def test_plugin_reference_never_reaches_import_eval_or_attribute_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = DefaultAdapterRegistry(
        scrapling=object(),
        official_api=object(),
        rental_housing=object(),
    )
    import_calls: list[object] = []
    eval_calls: list[object] = []
    attribute_calls: list[object] = []

    caught: MonitorError | None = None
    with monkeypatch.context() as guarded:
        guarded.setattr(
            importlib,
            "import_module",
            lambda *args, **kwargs: import_calls.append((args, kwargs)),
        )
        guarded.setattr(
            builtins,
            "eval",
            lambda *args, **kwargs: eval_calls.append((args, kwargs)),
        )
        guarded.setattr(
            builtins,
            "getattr",
            lambda *args, **kwargs: attribute_calls.append((args, kwargs)),
        )
        try:
            registry.resolve(
                SourceAdapterKind.PYTHON_PLUGIN,
                "package.module:factory.__call__",
            )
        except MonitorError as error:
            caught = error

    assert caught is not None
    assert import_calls == []
    assert eval_calls == []
    assert attribute_calls == []
