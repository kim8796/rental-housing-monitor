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


def test_registry_copies_python_plugin_allowlist_without_dynamic_fallback() -> None:
    scrapling = object()
    official = object()
    plugin = object()
    plugins = {"approved": plugin}
    registry = DefaultAdapterRegistry(
        scrapling=scrapling,
        official_api=official,
        python_plugins=plugins,
    )
    plugins["approved"] = object()
    plugins["later"] = object()

    assert registry.resolve(SourceAdapterKind.PYTHON_PLUGIN, "approved") is plugin
    with pytest.raises(MonitorError):
        registry.resolve(SourceAdapterKind.PYTHON_PLUGIN, "later")
    with pytest.raises(MonitorError) as caught:
        registry.resolve(SourceAdapterKind.PYTHON_PLUGIN, "secret.module:factory")

    assert "secret.module" not in str(caught.value)
    assert "secret.module" not in repr(caught.value)
