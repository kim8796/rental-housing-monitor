from __future__ import annotations

from collections.abc import Mapping

from personal_monitor.domain.spec import SourceAdapterKind
from personal_monitor.ports import SourceAdapter
from personal_monitor.security.url_policy import PolicyError


class DefaultAdapterRegistry:
    """Resolve only explicitly configured adapter keys."""

    def __init__(
        self,
        *,
        scrapling: SourceAdapter,
        official_api: SourceAdapter,
        python_plugins: Mapping[str, SourceAdapter] | None = None,
    ) -> None:
        self.scrapling = scrapling
        self.official_api = official_api
        self._python_plugins = dict(python_plugins or {})

    def resolve(self, kind: SourceAdapterKind, adapter_ref: str | None) -> SourceAdapter:
        if kind is SourceAdapterKind.SCRAPLING and adapter_ref is None:
            return self.scrapling
        if kind is SourceAdapterKind.OFFICIAL_API and adapter_ref == "json_get":
            return self.official_api
        if kind is SourceAdapterKind.PYTHON_PLUGIN and adapter_ref is not None:
            adapter = self._python_plugins.get(adapter_ref)
            if adapter is not None:
                return adapter
        raise PolicyError("source adapter is not allowed", stage="adapter_registry")
