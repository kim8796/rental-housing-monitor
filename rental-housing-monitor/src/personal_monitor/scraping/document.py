from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from personal_monitor.domain.spec import FetchStrategy


@dataclass(frozen=True, slots=True)
class SourceDocument:
    final_url: str = field(repr=False)
    status: int
    content_type: str
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    strategy: FetchStrategy
    redirect_urls: tuple[str, ...] = field(default=(), repr=False)
    redirect_location: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        object.__setattr__(self, "body", bytes(self.body))
        object.__setattr__(self, "redirect_urls", tuple(self.redirect_urls))
