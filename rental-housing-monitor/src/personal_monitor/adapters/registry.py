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
        rental_housing: SourceAdapter | None = None,
        python_plugins: Mapping[str, SourceAdapter] | None = None,
    ) -> None:
        self.scrapling = scrapling
        self.official_api = official_api
        configured = (python_plugins or {}).get("rental_housing")
        if (
            rental_housing is not None
            and configured is not None
            and rental_housing is not configured
        ):
            raise ValueError("rental_housing adapter is configured more than once")
        self._rental_housing = rental_housing if rental_housing is not None else configured

    def resolve(self, kind: SourceAdapterKind, adapter_ref: str | None) -> SourceAdapter:
        if kind is SourceAdapterKind.SCRAPLING and adapter_ref is None:
            return self.scrapling
        if kind is SourceAdapterKind.OFFICIAL_API and adapter_ref == "json_get":
            return self.official_api
        if (
            kind is SourceAdapterKind.PYTHON_PLUGIN
            and adapter_ref == "rental_housing"
            and self._rental_housing is not None
        ):
            return self._rental_housing
        raise PolicyError("source adapter is not allowed", stage="adapter_registry")
