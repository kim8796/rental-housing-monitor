from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence

import regex
from bs4 import BeautifulSoup, Tag
from scrapling import Selector

from personal_monitor.domain.observation import ObservedItem, Scalar, stable_item_id
from personal_monitor.domain.spec import ExtractSpec, FieldSpec
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.scraping.document import SourceDocument
from personal_monitor.scraping.normalizers import normalize_value

_JSON_INDEX = re.compile(r"0|[1-9][0-9]*")
_HIDDEN_STYLE = re.compile(
    r"(?:^|;)(?:display:none|visibility:(?:hidden|collapse))(?:!important)?(?:;|$)"
)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_MAX_INLINE_STYLE_LENGTH = 4096
_MAX_JSON_DEPTH = 100
_MAX_JSON_NODES = 100_000


class DeclarativeExtractor:
    """Extract only declared scalar fields from a bounded HTML or JSON document."""

    def extract(self, document: SourceDocument, spec: ExtractSpec) -> tuple[ObservedItem, ...]:
        if document.content_type in {"text/html", "application/xhtml+xml"}:
            rows = self._extract_html(document, spec)
        elif document.content_type == "application/json" or document.content_type.endswith("+json"):
            rows = self._extract_json(document, spec)
        else:
            raise MonitorError(
                ErrorClass.POLICY,
                "extract",
                "document content type is not extractable",
            )
        return tuple(_make_item(fields) for fields in rows)

    def _extract_html(self, document: SourceDocument, spec: ExtractSpec) -> list[dict[str, Scalar]]:
        try:
            page = Selector(document.body, url=document.final_url, adaptive=True)
            roots = _select(page, spec.item_scope, scoped=False)
        except Exception:
            raise _structure_error("item selector failed") from None

        rows: list[dict[str, Scalar]] = []
        for root in roots:
            fields: dict[str, Scalar] = {}
            for name, field in spec.fields.items():
                value = self._extract_html_field(root, field, document.final_url)
                if value is not None:
                    fields[name] = value
            rows.append(fields)
        return rows

    def _extract_html_field(self, root: Selector, field: FieldSpec, base_url: str) -> Scalar:
        try:
            matches = _select(root, field.selector, scoped=True)
        except Exception:
            raise _structure_error("field selector failed") from None
        if not matches:
            return _missing(field)
        if len(matches) != 1:
            raise _structure_error("field selector is ambiguous")

        match = matches[0]
        if field.attribute is not None:
            raw_value = match.attrib.get(field.attribute)
            if not isinstance(raw_value, str):
                return _missing(field)
        else:
            try:
                raw_value = _visible_text(match)
            except Exception:
                raise _structure_error("field text extraction failed") from None
        return _finish_field(raw_value, field, base_url)

    def _extract_json(self, document: SourceDocument, spec: ExtractSpec) -> list[dict[str, Scalar]]:
        payload = _strict_json(document.body)
        roots = _traverse_json(payload, spec.item_scope, required=True)
        if isinstance(roots, list):
            root_values = roots
        elif isinstance(roots, Mapping):
            root_values = [roots]
        else:
            raise _structure_error("item selector did not resolve to rows")

        rows: list[dict[str, Scalar]] = []
        for root in root_values:
            if not isinstance(root, Mapping):
                raise _structure_error("item selector did not resolve to objects")
            fields: dict[str, Scalar] = {}
            for name, field in spec.fields.items():
                if field.attribute is not None:
                    raise _validation_error("attributes are not supported for JSON")
                value = _traverse_json(root, field.selector, required=field.required)
                if value is _MISSING or value is None:
                    if field.required:
                        raise _structure_error("required field is missing")
                    continue
                if isinstance(value, bool):
                    raw_value = "true" if value else "false"
                elif isinstance(value, str | int | float):
                    raw_value = str(value)
                else:
                    raise _validation_error("JSON field is not scalar")
                fields[name] = _finish_field(raw_value, field, document.final_url)
            rows.append(fields)
        return rows


def _select(node: Selector, selector: str, *, scoped: bool) -> Sequence[Selector]:
    if selector.startswith(("/", "(", "./")):
        if scoped:
            detached = Selector(str(node.html_content), adaptive=False)
            roots = detached.css(node.tag)
            if not roots:
                raise ValueError("detached item root is unavailable")
            node = roots[0]
        xpath = _scope_xpath(selector) if scoped else selector
        return node.xpath(xpath)
    return node.css(selector)


def _scope_xpath(selector: str) -> str:
    result: list[str] = []
    quote: str | None = None
    previous_significant: str | None = None
    for character in selector:
        if quote is not None:
            result.append(character)
            if character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
            result.append(character)
            previous_significant = character
            continue
        if character == "/" and previous_significant in {None, "(", "|", "[", ",", "="}:
            result.append(".")
        result.append(character)
        if not character.isspace():
            previous_significant = character
    return "".join(result)


def _visible_text(match: Selector) -> str:
    try:
        soup = BeautifulSoup(str(match.html_content), "html.parser")
        for tag in reversed(soup.find_all(True)):
            if _is_hidden(tag):
                tag.decompose()
        return soup.get_text(separator=" ", strip=True)
    except Exception:
        raise _structure_error("field text extraction failed") from None


def _is_hidden(tag: Tag) -> bool:
    if tag.name in {"script", "style", "noscript", "template"} or tag.has_attr("hidden"):
        return True
    aria_hidden = tag.attrs.get("aria-hidden")
    if isinstance(aria_hidden, str) and aria_hidden.strip().casefold() == "true":
        return True
    style = tag.attrs.get("style")
    if not isinstance(style, str):
        return False
    if len(style) > _MAX_INLINE_STYLE_LENGTH:
        return True
    without_comments = _CSS_COMMENT.sub("", style)
    compact = re.sub(r"\s+", "", without_comments).casefold()
    return _HIDDEN_STYLE.search(compact) is not None


def _finish_field(raw_value: str, field: FieldSpec, base_url: str) -> Scalar:
    if not raw_value.strip():
        return _missing(field)
    captured = _capture(raw_value, field.pattern) if field.pattern is not None else raw_value
    return normalize_value(field.type, captured, base_url)


def _capture(value: str, pattern: str) -> str:
    try:
        match = regex.search(pattern, value, timeout=0.05)
    except (regex.error, TimeoutError):
        raise _validation_error("field pattern failed") from None
    if match is None:
        raise _validation_error("field pattern did not match")
    try:
        captured = match.group(1) if match.re.groups else match.group(0)
    except (IndexError, TypeError):
        raise _validation_error("field pattern capture failed") from None
    if not isinstance(captured, str) or not captured.strip():
        raise _validation_error("field pattern capture failed")
    return captured


def _strict_json(body: bytes) -> object:
    try:
        text = body.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _validate_json_tree(payload)
        return payload
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise _structure_error("JSON document is invalid") from None


def _validate_json_tree(payload: object) -> None:
    stack: list[tuple[object, int]] = [(payload, 0)]
    seen = 0
    while stack:
        value, depth = stack.pop()
        seen += 1
        if depth > _MAX_JSON_DEPTH or seen > _MAX_JSON_NODES:
            raise ValueError("JSON shape is too large")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        if isinstance(value, str):
            value.encode("utf-8")
        if isinstance(value, Mapping):
            for key in value:
                key.encode("utf-8")
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ValueError("non-finite constant")


class _Missing:
    pass


_MISSING = _Missing()


def _traverse_json(value: object, path: str, *, required: bool) -> object:
    try:
        segments = _json_segments(path)
    except ValueError:
        raise _validation_error("JSON selector is invalid") from None
    current = value
    for segment in segments:
        if isinstance(current, Mapping):
            if segment not in current:
                if required:
                    raise _structure_error("required field is missing")
                return _MISSING
            current = current[segment]
        elif isinstance(current, list):
            if not _JSON_INDEX.fullmatch(segment):
                raise _validation_error("JSON list selector is invalid")
            index = int(segment)
            if index >= len(current):
                if required:
                    raise _structure_error("required field is missing")
                return _MISSING
            current = current[index]
        else:
            if required:
                raise _structure_error("required field is missing")
            return _MISSING
    return current


def _json_segments(path: str) -> list[str]:
    if not path.startswith("/") or path == "/":
        raise ValueError("invalid slash path")
    segments = path[1:].split("/")
    if any(not segment or segment in {"*", "-"} for segment in segments):
        raise ValueError("invalid slash path")
    return segments


def _missing(field: FieldSpec) -> None:
    if field.required:
        raise _structure_error("required field is missing")
    return None


def _make_item(fields: Mapping[str, Scalar]) -> ObservedItem:
    try:
        return ObservedItem(stable_item_id(fields), fields)
    except Exception:
        raise _validation_error("item identity could not be generated") from None


def _structure_error(detail: str) -> MonitorError:
    return MonitorError(ErrorClass.STRUCTURE, "extract", detail)


def _validation_error(detail: str) -> MonitorError:
    return MonitorError(ErrorClass.VALIDATION, "extract", detail)
