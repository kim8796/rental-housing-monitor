from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from scrapling import Selector

from personal_monitor.domain.observation import ObservedItem, Scalar
from personal_monitor.domain.spec import (
    SENSITIVE_QUERY_PARAMETER_NAMES,
    MonitorSpec,
    SourceAdapterKind,
)
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.scraping.adaptive_storage import EncryptedAdaptiveStorage
from personal_monitor.scraping.document import SourceDocument
from personal_monitor.scraping.extractor import DeclarativeExtractor, _scope_xpath
from personal_monitor.scraping.validator import ObservationValidator
from personal_monitor.security.encryption import AesGcmCipher, EncryptedBlob
from personal_monitor.security.sanitize import sanitize_for_ai
from personal_monitor.security.url_policy import UrlPolicy
from personal_monitor.storage.recovery import RecoveryRepository
from personal_monitor.storage.registry import ActiveMonitor, RegistryRepository

if TYPE_CHECKING:
    from scrapling import Selector as ScraplingSelector

MAX_FIELD_CHANGES = 20
MAX_PREVIEW_ITEMS = 5
MAX_PREVIEW_FIELDS = 8
MAX_PREVIEW_TEXT = 160
MAX_SELECTOR_LENGTH = 500
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_ADAPTIVE_FAILURES = frozenset({ErrorClass.STRUCTURE, ErrorClass.VALIDATION})
_SENSITIVE_FIELD = re.compile(
    r"(?:auth|cookie|credential|key|pass(?:word|wd)?|secret|session|token)", re.IGNORECASE
)
_CREDENTIAL_TEXT = re.compile(
    r"\b(?:access[_-]?token|api[_-]?key|authorization|cookie|credential|passwd|password|"
    r"secret|session(?:id)?|token)\s*[=:]",
    re.IGNORECASE,
)
_SAFE_SELECTOR_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")


@dataclass(frozen=True, slots=True)
class RecoveryCandidate:
    version_id: str = field(repr=False)
    validation_passed: bool
    field_changes: Mapping[str, str] = field(repr=False)
    preview_items: Sequence[Mapping[str, Scalar]] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.version_id, str) or not self.version_id:
            raise ValueError("candidate version is invalid")
        if not isinstance(self.validation_passed, bool):
            raise TypeError("candidate validation state is invalid")
        changes: dict[str, str] = {}
        for name, selector in sorted(self.field_changes.items())[:MAX_FIELD_CHANGES]:
            if not isinstance(name, str) or not isinstance(selector, str):
                raise TypeError("candidate field changes are invalid")
            if _unsafe_text(selector, ()):
                continue
            changes[name[:64]] = selector[:MAX_SELECTOR_LENGTH]
        previews: list[Mapping[str, Scalar]] = []
        for item in self.preview_items[:MAX_PREVIEW_ITEMS]:
            if not isinstance(item, Mapping):
                raise TypeError("candidate preview is invalid")
            bounded: dict[str, Scalar] = {}
            for name, value in sorted(item.items()):
                if len(bounded) >= MAX_PREVIEW_FIELDS:
                    break
                if not isinstance(name, str) or _SENSITIVE_FIELD.search(name):
                    continue
                if not isinstance(value, str | int | float | bool) or value is None:
                    continue
                if isinstance(value, str) and _unsafe_text(value, ()):
                    continue
                bounded[name[:64]] = value[:MAX_PREVIEW_TEXT] if isinstance(value, str) else value
            previews.append(MappingProxyType(bounded))
        object.__setattr__(self, "field_changes", MappingProxyType(changes))
        object.__setattr__(self, "preview_items", tuple(previews))

    def __repr__(self) -> str:
        return (
            "RecoveryCandidate("
            f"validation_passed={self.validation_passed!r}, "
            f"field_change_count={len(self.field_changes)}, "
            f"preview_item_count={len(self.preview_items)})"
        )


class AdaptiveRecovery:
    """Build review-only adaptive candidates using one explicitly supplied feature store."""

    def __init__(
        self,
        *,
        registry: RegistryRepository,
        repository: RecoveryRepository,
        cipher: AesGcmCipher,
        adaptive_storage: EncryptedAdaptiveStorage,
        url_policy: UrlPolicy,
        extractor: DeclarativeExtractor,
        validator: ObservationValidator,
    ) -> None:
        if not isinstance(registry, RegistryRepository):
            raise TypeError("registry is invalid")
        if not isinstance(repository, RecoveryRepository):
            raise TypeError("recovery repository is invalid")
        if not isinstance(cipher, AesGcmCipher):
            raise TypeError("cipher is invalid")
        if type(adaptive_storage) is not EncryptedAdaptiveStorage:
            raise TypeError("adaptive storage must be the sealed encrypted store")
        if type(url_policy) is not UrlPolicy:
            raise TypeError("URL policy is invalid")
        if not isinstance(extractor, DeclarativeExtractor):
            raise TypeError("extractor is invalid")
        if not isinstance(validator, ObservationValidator):
            raise TypeError("validator is invalid")
        self._registry = registry
        self._repository = repository
        self._cipher = cipher
        self._adaptive_storage = adaptive_storage
        self._url_policy = url_policy
        self._extractor = extractor
        self._validator = validator

    async def save_success_baseline(
        self,
        monitor_id: str,
        *,
        owner_id: str,
        document: SourceDocument,
    ) -> None:
        active = await self._eligible_active(monitor_id, owner_id, document)
        items = self._extractor.extract(document, active.spec.extract)
        self._validator.validate(items, active.spec.extract, active.spec.validators)
        page = self._page(document, active)
        roots = _select(page, active.spec.extract.item_scope)
        if not roots:
            raise ValueError("baseline selectors are unavailable")
        baseline_fields: list[tuple[str, ScraplingSelector]] = []
        for index, root in enumerate(roots):
            for name, field_spec in active.spec.extract.fields.items():
                matches = _select(root, field_spec.selector, scoped=True)
                if len(matches) != 1:
                    raise ValueError("baseline selectors are unavailable")
                baseline_fields.append((f"field:{name}:{index}", matches[0]))
        for index, root in enumerate(roots):
            page.save(root, f"item_scope:{index}")
        for identifier, match in baseline_fields:
            page.save(match, identifier)

    async def propose_adaptive(
        self,
        monitor_id: str,
        *,
        owner_id: str,
        document: SourceDocument,
        failure_class: ErrorClass,
        secret_values: Iterable[str] = (),
    ) -> RecoveryCandidate | None:
        if failure_class not in _ADAPTIVE_FAILURES or document.content_type not in _HTML_TYPES:
            raise ValueError("failure is not eligible for adaptive recovery")
        active = await self._eligible_active(monitor_id, owner_id, document)
        secrets = _copy_secrets(secret_values)
        diagnostic = self._diagnostic_blob(monitor_id, document, secrets)
        try:
            proposal = self._build_proposal(active, document, secrets)
        except Exception:
            proposal = None
        if proposal is None:
            self._repository.store_diagnostic(monitor_id, owner_id, diagnostic)
            return None
        proposed_spec, field_changes = proposal
        try:
            items = self._extractor.extract(document, proposed_spec.extract)
            validated = self._validator.validate(
                items,
                proposed_spec.extract,
                proposed_spec.validators,
            )
        except MonitorError:
            self._repository.store_diagnostic(monitor_id, owner_id, diagnostic)
            return None
        if _unsafe_items(validated, secrets):
            self._repository.store_diagnostic(monitor_id, owner_id, diagnostic)
            return None
        version_id = self._repository.store_candidate(
            monitor_id=monitor_id,
            owner_id=owner_id,
            expected_active_version_id=active.version_id,
            spec=proposed_spec,
            diagnostic=diagnostic,
        )
        return RecoveryCandidate(
            version_id=version_id,
            validation_passed=True,
            field_changes=field_changes,
            preview_items=_preview(validated, secrets),
        )

    def _build_proposal(
        self,
        active: ActiveMonitor,
        document: SourceDocument,
        secrets: tuple[str, ...],
    ) -> tuple[MonitorSpec, Mapping[str, str]] | None:
        page = self._page(document, active)
        original = active.spec.extract.item_scope
        roots = _select(page, original)
        changes: dict[str, str] = {}
        if not roots:
            namespace = self._adaptive_storage.for_namespace(
                owner_id=active.owner_id,
                monitor_id=active.id,
                version_id=active.version_id,
            )
            exemplar_count = 0
            relocated_roots: list[ScraplingSelector] = []
            for index in range(active.spec.validators.max_items):
                identifier = f"item_scope:{index}"
                if namespace.retrieve(identifier) is None:
                    break
                exemplar_count += 1
                relocated = _select(
                    page,
                    original,
                    identifier=identifier,
                    adaptive=True,
                )
                relocated_roots.extend(relocated)
            if exemplar_count == 0 or not relocated_roots:
                return None
            generated = _generalized_selector_groups(
                page,
                relocated_roots,
                xpath=_is_xpath(original),
                expected_count=exemplar_count,
            )
            if generated is None:
                return None
            roots = _select(page, generated)
            changes["item_scope"] = generated

        generated_fields: dict[str, str] = {}
        for name, field_spec in active.spec.extract.fields.items():
            original_field = field_spec.selector
            matches_by_root = [_select(root, original_field, scoped=True) for root in roots]
            if all(len(matches) == 1 for matches in matches_by_root):
                continue
            if any(len(matches) > 1 for matches in matches_by_root):
                return None
            first_relocated = None
            for index, (root, matches) in enumerate(zip(roots, matches_by_root, strict=True)):
                if matches:
                    continue
                relocated = _select(
                    root,
                    original_field,
                    identifier=f"field:{name}:{index}",
                    adaptive=True,
                    scoped=True,
                )
                if len(relocated) != 1:
                    return None
                first_relocated = first_relocated or (root, relocated[0])
            if first_relocated is None:
                return None
            generated = _generated_scoped_selector(
                first_relocated[0],
                first_relocated[1],
                xpath=_is_xpath(original_field),
            )
            if not all(_unique_match(root, generated, scoped=True) for root in roots):
                return None
            generated_fields[name] = generated
            changes[f"field:{name}"] = generated

        if not changes:
            return None
        if any(_unsafe_text(selector, secrets) for selector in changes.values()):
            return None
        payload = active.spec.model_dump(mode="python")
        extract = dict(payload["extract"])
        if "item_scope" in changes:
            extract["item_scope"] = changes["item_scope"]
        fields = {name: dict(value) for name, value in extract["fields"].items()}
        for name, generated in generated_fields.items():
            fields[name]["selector"] = generated
        extract["fields"] = fields
        payload["extract"] = extract
        return MonitorSpec.model_validate(payload), MappingProxyType(changes)

    async def _eligible_active(
        self,
        monitor_id: str,
        owner_id: str,
        document: SourceDocument,
    ) -> ActiveMonitor:
        if not isinstance(document, SourceDocument) or document.content_type not in _HTML_TYPES:
            raise ValueError("document is not eligible for adaptive recovery")
        try:
            active = self._registry.get_active_monitor_for_recovery(monitor_id, owner_id=owner_id)
        except ValueError:
            raise ValueError("monitor is not eligible for adaptive recovery") from None
        if active.spec.source_adapter is not SourceAdapterKind.SCRAPLING:
            raise ValueError("monitor is not eligible for adaptive recovery")
        await self._validate_document_lineage(active, document)
        return active

    async def _validate_document_lineage(
        self, active: ActiveMonitor, document: SourceDocument
    ) -> None:
        try:
            requested = await self._url_policy.validate(active.spec.target_url)
            approved_chain: list[str] = []
            for redirect_count, redirect_url in enumerate(document.redirect_urls, start=1):
                target = await self._url_policy.validate_redirect(
                    redirect_url, redirect_count=redirect_count
                )
                if redirect_count == 1 and target.normalized_url != requested.normalized_url:
                    raise ValueError
                if target.normalized_url in approved_chain:
                    raise ValueError
                approved_chain.append(target.normalized_url)
            final = await self._url_policy.validate(document.final_url)
            if approved_chain:
                if final.normalized_url in approved_chain:
                    raise ValueError
            elif final.normalized_url != requested.normalized_url:
                raise ValueError
        except Exception:
            raise ValueError("document is not eligible for adaptive recovery") from None

    def _page(self, document: SourceDocument, active: ActiveMonitor) -> Selector:
        storage = self._adaptive_storage.for_namespace(
            owner_id=active.owner_id,
            monitor_id=active.id,
            version_id=active.version_id,
        )
        return Selector(
            document.body,
            url=document.final_url,
            adaptive=True,
            _storage=storage,
        )

    def _diagnostic_blob(
        self,
        monitor_id: str,
        document: SourceDocument,
        secrets: tuple[str, ...],
    ) -> EncryptedBlob:
        html = document.body.decode("utf-8", errors="replace")
        sanitized = sanitize_for_ai(html, secret_values=secrets)
        return self._cipher.encrypt(sanitized.encode("utf-8"), monitor_id.encode())


def _select(
    node: ScraplingSelector,
    selector: str,
    *,
    identifier: str = "",
    adaptive: bool = False,
    auto_save: bool = False,
    scoped: bool = False,
) -> Sequence[ScraplingSelector]:
    if _is_xpath(selector):
        expression = _scope_xpath(selector) if scoped else selector
        return node.xpath(
            expression,
            identifier=identifier,
            adaptive=adaptive,
            auto_save=auto_save,
            percentage=25,
        )
    return node.css(
        selector,
        identifier=identifier,
        adaptive=adaptive,
        auto_save=auto_save,
        percentage=25,
    )


def _is_xpath(selector: str) -> bool:
    return selector.startswith(("/", "(", "./"))


def _generated_selector(element: ScraplingSelector, *, xpath: bool) -> str:
    generated = element.generate_xpath_selector if xpath else element.generate_css_selector
    return _bounded_selector(generated)


def _generalized_selector(
    page: ScraplingSelector,
    elements: Sequence[ScraplingSelector],
    *,
    xpath: bool,
    expected_count: int,
) -> str | None:
    unique_elements = {element.generate_full_xpath_selector: element for element in elements}
    if not unique_elements:
        return None
    values = tuple(unique_elements.values())
    tag = values[0].tag
    if not isinstance(tag, str) or _SAFE_SELECTOR_TOKEN.fullmatch(tag) is None:
        return None
    if any(element.tag != tag for element in values):
        return None
    common_attributes = {
        name: value
        for name, value in values[0].attrib.items()
        if isinstance(name, str)
        and isinstance(value, str)
        and _SAFE_SELECTOR_TOKEN.fullmatch(name)
        and _SAFE_SELECTOR_TOKEN.fullmatch(value)
        and all(element.attrib.get(name) == value for element in values[1:])
    }
    classes = tuple(
        sorted(
            token
            for token in common_attributes.pop("class", "").split()
            if _SAFE_SELECTOR_TOKEN.fullmatch(token)
        )
    )
    candidates: list[str] = []
    if xpath:
        predicates = [f"@{name}='{value}'" for name, value in sorted(common_attributes.items())]
        predicates.extend(
            f"contains(concat(' ', normalize-space(@class), ' '), ' {token} ')" for token in classes
        )
        if predicates:
            candidates.append(f"//{tag}[{' and '.join(predicates)}]")
        candidates.append(f"//{tag}")
    else:
        base = tag + "".join(f".{token}" for token in classes)
        base += "".join(f'[{name}="{value}"]' for name, value in sorted(common_attributes.items()))
        if base != tag:
            candidates.append(base)
        candidates.append(tag)
    relocated_paths = set(unique_elements)
    for candidate in candidates:
        bounded = _bounded_selector(candidate)
        matches = _select(page, bounded)
        match_paths = {match.generate_full_xpath_selector for match in matches}
        if len(matches) == expected_count and relocated_paths <= match_paths:
            return bounded
    return None


def _generalized_selector_groups(
    page: ScraplingSelector,
    elements: Sequence[ScraplingSelector],
    *,
    xpath: bool,
    expected_count: int,
) -> str | None:
    by_tag: dict[str, list[ScraplingSelector]] = {}
    for element in elements:
        if isinstance(element.tag, str):
            by_tag.setdefault(element.tag, []).append(element)
    candidates: list[str] = []
    for tag in sorted(by_tag):
        generated = _generalized_selector(
            page,
            by_tag[tag],
            xpath=xpath,
            expected_count=expected_count,
        )
        if generated is not None:
            candidates.append(generated)
    return min(candidates, key=lambda value: (len(value), value)) if candidates else None


def _generated_scoped_selector(
    root: ScraplingSelector,
    element: ScraplingSelector,
    *,
    xpath: bool,
) -> str:
    if xpath:
        root_prefix = root.generate_full_xpath_selector.rstrip("/")
        element_selector = element.generate_full_xpath_selector
        separator = "/"
        if not element_selector.startswith(root_prefix + separator):
            raise ValueError("generated selector is outside its item scope")
        relative = "." + element_selector[len(root_prefix) :]
    else:
        root_prefix = root.generate_full_css_selector.rstrip()
        element_selector = element.generate_full_css_selector
        separator = " > "
        if not element_selector.startswith(root_prefix + separator):
            raise ValueError("generated selector is outside its item scope")
        relative = element_selector[len(root_prefix + separator) :]
    if not relative:
        raise ValueError("generated selector is empty")
    return _bounded_selector(relative)


def _bounded_selector(selector: object) -> str:
    if not isinstance(selector, str) or not selector or len(selector) > MAX_SELECTOR_LENGTH:
        raise ValueError("generated selector is invalid")
    return selector


def _unique_match(node: ScraplingSelector, selector: str, *, scoped: bool = False) -> bool:
    try:
        return len(_select(node, selector, scoped=scoped)) == 1
    except Exception:
        return False


def _copy_secrets(secret_values: Iterable[str]) -> tuple[str, ...]:
    if secret_values is None or isinstance(secret_values, str | bytes):
        raise TypeError("secret values are invalid")
    values = tuple(secret_values)
    if any(not isinstance(value, str) for value in values):
        raise TypeError("secret values are invalid")
    return tuple(sorted({value for value in values if value}, key=lambda item: (-len(item), item)))


def _preview(items: Sequence[ObservedItem], secrets: tuple[str, ...]) -> list[Mapping[str, Scalar]]:
    previews: list[Mapping[str, Scalar]] = []
    for item in items[:MAX_PREVIEW_ITEMS]:
        values: dict[str, Scalar] = {}
        for name, value in item.fields.items():
            if _SENSITIVE_FIELD.search(name):
                continue
            values[name] = _sanitize_preview_value(value, secrets)
        previews.append(values)
    return previews


def _unsafe_items(items: Sequence[ObservedItem], secrets: tuple[str, ...]) -> bool:
    return any(
        isinstance(value, str) and _unsafe_text(value, secrets)
        for item in items
        for value in item.fields.values()
    )


def _unsafe_text(value: str, secrets: tuple[str, ...]) -> bool:
    if any(secret in value for secret in secrets) or _CREDENTIAL_TEXT.search(value):
        return True
    normalized = value.casefold()
    if not normalized.startswith(("http://", "https://")):
        return re.match(r"^//[^/\[\]()\s]+@", normalized) is not None
    try:
        parts = urlsplit(value)
        query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=100)
    except (UnicodeError, ValueError):
        return True
    return parts.scheme.casefold() in {"http", "https"} and (
        parts.username is not None
        or parts.password is not None
        or any(name.casefold() in SENSITIVE_QUERY_PARAMETER_NAMES for name, _value in query)
    )


def _sanitize_preview_value(value: Scalar, secrets: tuple[str, ...]) -> Scalar:
    if not isinstance(value, str):
        return value
    result = value
    for secret in secrets:
        result = result.replace(secret, "")
    try:
        parts = urlsplit(result)
        if parts.scheme in {"http", "https"}:
            if parts.username is not None or parts.password is not None:
                return ""
            result = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return ""
    return result[:MAX_PREVIEW_TEXT]
