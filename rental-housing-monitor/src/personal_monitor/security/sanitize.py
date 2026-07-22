from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import SplitResult, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

MAX_SANITIZED_CHARACTERS = 40_000
_ACTIVE_TAGS = frozenset({"script", "style", "noscript", "template"})
_FORM_CONTROLS = frozenset({"button", "datalist", "input", "option", "select", "textarea"})
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|\Z)", re.DOTALL)
_HIDDEN_STYLE = re.compile(
    r"(?:\A|;)display:none(?:!important)?(?:;|\Z)"
    r"|(?:\A|;)visibility:(?:hidden|collapse)(?:!important)?(?:;|\Z)"
)
_CREDENTIAL_TEXT = re.compile(
    r"(?:\bcookie\s*:|\b(?:access[_-]?token|api[_-]?key|authorization|bearer|"
    r"client[_-]?secret|credential|passwd|password|secret|session(?:id)?|token)\s*[=:])",
    re.IGNORECASE,
)
_VALID_PERCENT = re.compile(r"%(?:[0-9A-Fa-f]{2})")
_DUPLICATE_ATTRIBUTE = "__pm_duplicate_attribute__"
_VISIBILITY_ATTRIBUTES = frozenset({"aria-hidden", "hidden", "style"})


def sanitize_for_ai(html: str, *, secret_values: Iterable[str] = ()) -> str:
    """Return a deterministic, bounded visible HTML fragment safe for diagnostic AI use."""
    if not isinstance(html, str):
        raise TypeError("html must be a string")
    secrets = _copy_secrets(secret_values)
    normalized = html.encode("utf-8", errors="replace").decode("utf-8")
    soup = BeautifulSoup(
        normalized,
        "html.parser",
        on_duplicate_attribute=_mark_duplicate_attribute,
    )

    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for tag in list(soup.find_all(True)):
        if tag.attrs is None:
            continue
        duplicate = tag.attrs.get(_DUPLICATE_ATTRIBUTE)
        if isinstance(duplicate, str) and any(
            name in _VISIBILITY_ATTRIBUTES for name in duplicate.split(",")
        ):
            tag.decompose()
            continue
        if tag.name in _ACTIVE_TAGS or tag.name in _FORM_CONTROLS or _is_hidden(tag):
            tag.decompose()
    for form in list(soup.find_all("form")):
        form.unwrap()

    for text in list(soup.find_all(string=True)):
        if not isinstance(text, NavigableString):
            continue
        if _CREDENTIAL_TEXT.search(str(text)):
            parent = text.parent
            if isinstance(parent, Tag):
                parent.decompose()
            else:
                text.extract()
            continue
        if not str(text).strip():
            text.extract()

    for tag in soup.find_all(True):
        _sanitize_attributes(tag, secrets)

    result = "".join(str(node) for node in soup.contents)
    for secret in secrets:
        result = result.replace(secret, "")
    return result[:MAX_SANITIZED_CHARACTERS]


def _copy_secrets(secret_values: Iterable[str]) -> tuple[str, ...]:
    if secret_values is None or isinstance(secret_values, str | bytes):
        raise TypeError("secret_values must be an iterable of strings")
    try:
        values = tuple(secret_values)
    except TypeError:
        raise TypeError("secret_values must be an iterable of strings") from None
    if any(not isinstance(value, str) for value in values):
        raise TypeError("secret_values must contain only strings")
    return tuple(
        sorted({value for value in values if value}, key=lambda value: (-len(value), value))
    )


def _is_hidden(tag: Tag) -> bool:
    if tag.has_attr("hidden"):
        return True
    aria_hidden = tag.attrs.get("aria-hidden")
    if isinstance(aria_hidden, str) and aria_hidden.strip().casefold() == "true":
        return True
    style = tag.attrs.get("style")
    if not isinstance(style, str):
        return False
    compact = re.sub(r"\s+", "", _CSS_COMMENT.sub("", style)).casefold()
    return _HIDDEN_STYLE.search(compact) is not None


def _sanitize_attributes(tag: Tag, secrets: tuple[str, ...]) -> None:
    original = dict(tag.attrs)
    if _DUPLICATE_ATTRIBUTE in original:
        tag.attrs = {}
        return
    safe: dict[str, str | list[str]] = {}
    identifier = original.get("id")
    if isinstance(identifier, str) and _safe_value(identifier, secrets):
        safe["id"] = identifier

    classes = original.get("class")
    class_values = [classes] if isinstance(classes, str) else classes
    if isinstance(class_values, list):
        kept = [
            value
            for value in class_values[:20]
            if isinstance(value, str) and _safe_value(value, secrets)
        ]
        if kept:
            safe["class"] = kept

    href = original.get("href")
    if isinstance(href, str) and not any(secret in href for secret in secrets):
        sanitized_href = _sanitize_href(href)
        if sanitized_href is not None:
            safe["href"] = sanitized_href
    tag.attrs = safe


def _safe_value(value: str, secrets: tuple[str, ...]) -> bool:
    return _SAFE_TOKEN.fullmatch(value) is not None and not any(
        secret in value for secret in secrets
    )


def _sanitize_href(value: str) -> str | None:
    if len(value) > 2048 or any(character.isspace() for character in value):
        return None
    if "\\" in value or _has_invalid_percent(value):
        return None
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        return None
    if parts.scheme and parts.scheme.casefold() not in {"http", "https"}:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.netloc and parts.hostname is None:
        return None
    if "%" in parts.netloc:
        return None
    if not parts.scheme and parts.netloc:
        return None
    if not parts.scheme and not value.startswith(("/", "./", "../")):
        return None
    sanitized = SplitResult(parts.scheme.casefold(), parts.netloc, parts.path, "", "")
    result = urlunsplit(sanitized)
    return result if result else None


def _has_invalid_percent(value: str) -> bool:
    index = 0
    while True:
        index = value.find("%", index)
        if index < 0:
            return False
        if _VALID_PERCENT.match(value, index) is None:
            return True
        index += 3


def _mark_duplicate_attribute(attributes: dict[str, str], name: str, _value: str) -> None:
    duplicates = set(filter(None, attributes.get(_DUPLICATE_ATTRIBUTE, "").split(",")))
    duplicates.add(name.casefold())
    attributes[_DUPLICATE_ATTRIBUTE] = ",".join(sorted(duplicates))
    attributes.pop(name, None)
