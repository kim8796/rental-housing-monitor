from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from lxml.html import HtmlElement
from scrapling.core.storage import StorageSystemMixin

from personal_monitor.security.encryption import AesGcmCipher, EncryptedBlob
from personal_monitor.storage.schema import canonical_json, transaction, utc_timestamp

_IDENTIFIER = re.compile(r"(?:item_scope:[0-9]{1,6}|field:[a-z][a-z0-9_]{0,63}:[0-9]{1,6})\Z")
_NAMESPACE_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}\Z")
_FORMAT = "personal-monitor-adaptive-feature-v1"
_AAD_PREFIX = b"personal-monitor/adaptive-feature/v1\0"
_MAX_FEATURE_BYTES = 32_768
_MAX_ATTRIBUTES = 64
_MAX_ATTRIBUTE_NAME = 256
_MAX_ATTRIBUTE_VALUE = 4_096
_MAX_TEXT = 8_192
_MAX_TAG = 128
_MAX_PATH = 64
_MAX_RELATED = 128
_RETENTION = timedelta(days=90)


class EncryptedAdaptiveStorage:
    """Factory for monitor/version-scoped authenticated Scrapling feature stores."""

    __slots__ = ("_connection", "_cipher", "_clock", "_composition", "_sealed")

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        cipher: AesGcmCipher,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be a SQLite connection")
        if type(cipher) is not AesGcmCipher:
            raise TypeError("cipher must be an AesGcmCipher")
        if not callable(clock):
            raise TypeError("clock must be callable")
        _clock_value(clock)
        try:
            if connection.row_factory is not sqlite3.Row or connection.isolation_level is not None:
                raise RuntimeError
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise RuntimeError
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(adaptive_features)")
            }
            if columns != {
                "key_hash",
                "namespace_hash",
                "nonce",
                "ciphertext",
                "created_at",
                "updated_at",
                "expires_at",
            }:
                raise RuntimeError
            connection.execute("SELECT 1 FROM adaptive_features LIMIT 1")
        except (RuntimeError, sqlite3.Error):
            raise RuntimeError("adaptive feature storage is unavailable") from None
        self._connection = connection
        self._cipher = cipher
        self._clock = clock
        self._composition = (connection, cipher, clock)
        self._sealed = True

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("adaptive feature storage is sealed")
        object.__setattr__(self, name, value)

    def __repr__(self) -> str:
        return "EncryptedAdaptiveStorage()"

    def for_namespace(
        self, *, owner_id: str, monitor_id: str, version_id: str
    ) -> EncryptedAdaptiveNamespace:
        self._assert_integrity()
        parts = tuple(_namespace_part(value) for value in (owner_id, monitor_id, version_id))
        namespace = b"\0".join(part.encode("utf-8") for part in parts)
        return EncryptedAdaptiveNamespace(
            self._connection,
            cipher=self._cipher,
            clock=self._clock,
            namespace=namespace,
        )

    def purge_expired(self, *, now: datetime) -> int:
        self._assert_integrity()
        timestamp = utc_timestamp(now, parameter="now")
        try:
            with transaction(self._connection, immediate=True):
                cursor = self._connection.execute(
                    "DELETE FROM adaptive_features WHERE expires_at <= ?", (timestamp,)
                )
            return cursor.rowcount
        except sqlite3.Error:
            raise RuntimeError("adaptive feature storage failed") from None

    def _assert_integrity(self) -> None:
        try:
            connection, cipher, clock = self._composition
            valid = (
                self._connection is connection
                and self._cipher is cipher
                and self._clock is clock
                and type(self._cipher) is AesGcmCipher
                and isinstance(self._connection, sqlite3.Connection)
            )
        except (AttributeError, TypeError, ValueError):
            valid = False
        if not valid:
            raise RuntimeError("adaptive feature storage integrity check failed")


class EncryptedAdaptiveNamespace(StorageSystemMixin):
    """A sealed namespace implementing Scrapling's adaptive storage contract."""

    __slots__ = (
        "_connection",
        "_cipher",
        "_clock",
        "_namespace",
        "_namespace_hash",
        "_composition",
        "_sealed",
    )

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        cipher: AesGcmCipher,
        clock: Callable[[], datetime],
        namespace: bytes,
    ) -> None:
        super().__init__(None)
        self._connection = connection
        self._cipher = cipher
        self._clock = clock
        self._namespace = bytes(namespace)
        self._namespace_hash = hashlib.sha256(self._namespace).hexdigest()
        self._composition = (
            connection,
            cipher,
            clock,
            self._namespace,
            self._namespace_hash,
        )
        self._sealed = True

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("adaptive feature namespace is sealed")
        object.__setattr__(self, name, value)

    def __repr__(self) -> str:
        return "EncryptedAdaptiveNamespace()"

    def save(self, element: HtmlElement, identifier: str) -> None:
        self._assert_integrity()
        checked_identifier = _identifier(identifier)
        feature = _element_feature(element)
        payload = _encode_feature(feature)
        aad = self._aad(checked_identifier)
        blob = self._cipher.encrypt(payload, aad)
        now = _clock_value(self._clock)
        timestamp = now.isoformat()
        expires_at = (now + _RETENTION).isoformat()
        key_hash = hashlib.sha256(aad).hexdigest()
        try:
            with transaction(self._connection, immediate=True):
                self._connection.execute(
                    "INSERT INTO adaptive_features("
                    "key_hash, namespace_hash, nonce, ciphertext, created_at, updated_at, "
                    "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(key_hash) DO UPDATE SET "
                    "namespace_hash = excluded.namespace_hash, nonce = excluded.nonce, "
                    "ciphertext = excluded.ciphertext, updated_at = excluded.updated_at, "
                    "expires_at = excluded.expires_at",
                    (
                        key_hash,
                        self._namespace_hash,
                        blob.nonce,
                        blob.ciphertext,
                        timestamp,
                        timestamp,
                        expires_at,
                    ),
                )
        except sqlite3.Error:
            raise RuntimeError("adaptive feature storage failed") from None

    def retrieve(self, identifier: str) -> dict[str, object] | None:
        self._assert_integrity()
        checked_identifier = _identifier(identifier)
        aad = self._aad(checked_identifier)
        key_hash = hashlib.sha256(aad).hexdigest()
        try:
            row = self._connection.execute(
                "SELECT nonce, ciphertext, expires_at FROM adaptive_features "
                "WHERE key_hash = ? AND namespace_hash = ?",
                (key_hash, self._namespace_hash),
            ).fetchone()
        except sqlite3.Error:
            raise RuntimeError("adaptive feature storage failed") from None
        if row is None:
            return None
        try:
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                raise ValueError
            if expires_at.astimezone(UTC) <= _clock_value(self._clock):
                self.delete(checked_identifier)
                return None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("adaptive feature authentication failed") from None
        try:
            plaintext = self._cipher.decrypt(
                EncryptedBlob(nonce=row["nonce"], ciphertext=row["ciphertext"]), aad
            )
        except (TypeError, ValueError):
            raise ValueError("adaptive feature authentication failed") from None
        try:
            return _decode_feature(plaintext)
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
            raise ValueError("adaptive feature payload is invalid") from None

    def delete(self, identifier: str) -> bool:
        self._assert_integrity()
        checked_identifier = _identifier(identifier)
        aad = self._aad(checked_identifier)
        key_hash = hashlib.sha256(aad).hexdigest()
        try:
            with transaction(self._connection, immediate=True):
                cursor = self._connection.execute(
                    "DELETE FROM adaptive_features WHERE key_hash = ? AND namespace_hash = ?",
                    (key_hash, self._namespace_hash),
                )
            return cursor.rowcount == 1
        except sqlite3.Error:
            raise RuntimeError("adaptive feature storage failed") from None

    def delete_all(self) -> int:
        self._assert_integrity()
        try:
            with transaction(self._connection, immediate=True):
                cursor = self._connection.execute(
                    "DELETE FROM adaptive_features WHERE namespace_hash = ?",
                    (self._namespace_hash,),
                )
            return cursor.rowcount
        except sqlite3.Error:
            raise RuntimeError("adaptive feature storage failed") from None

    def _aad(self, identifier: str) -> bytes:
        return _AAD_PREFIX + self._namespace + b"\0" + identifier.encode("ascii")

    def _assert_integrity(self) -> None:
        try:
            connection, cipher, clock, namespace, namespace_hash = self._composition
            valid = (
                self._connection is connection
                and self._cipher is cipher
                and self._clock is clock
                and self._namespace == namespace
                and self._namespace_hash == namespace_hash
                and hashlib.sha256(self._namespace).hexdigest() == self._namespace_hash
                and self.url is None
                and type(self._cipher) is AesGcmCipher
                and isinstance(self._connection, sqlite3.Connection)
            )
        except (AttributeError, TypeError, ValueError):
            valid = False
        if not valid:
            raise RuntimeError("adaptive feature storage integrity check failed")


def _namespace_part(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("adaptive namespace values must be strings")
    if not _NAMESPACE_PART.fullmatch(value):
        raise ValueError("adaptive namespace value is invalid")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("adaptive identifier must be a string")
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("adaptive identifier is invalid")
    return value


def _clock_value(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime):
        raise TypeError("clock must return a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _element_feature(element: object) -> dict[str, object]:
    if not isinstance(element, HtmlElement):
        raise TypeError("adaptive element must be an HTML element")
    tag = _tag(element.tag)
    attributes = _attributes(element.attrib)
    feature: dict[str, object] = {
        "tag": tag,
        "attributes": attributes,
        "text": _text(element.text),
        "path": _path(element),
    }
    parent = element.getparent()
    if parent is not None:
        feature.update(
            {
                "parent_name": _tag(parent.tag),
                "parent_attribs": _attributes(parent.attrib),
                "parent_text": _text(parent.text),
            }
        )
        siblings = [_tag(child.tag) for child in parent.iterchildren() if child is not element]
        if siblings:
            feature["siblings"] = _related(siblings)
    children = [_tag(child.tag) for child in element.iterchildren()]
    if children:
        feature["children"] = _related(children)
    _validate_feature(feature)
    return feature


def _tag(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_TAG:
        raise ValueError("adaptive feature must be bounded")
    return value


def _attributes(value: Mapping[object, object]) -> dict[str, str]:
    if len(value) > _MAX_ATTRIBUTES:
        raise ValueError("adaptive feature must be bounded")
    result: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise ValueError("adaptive feature must be bounded")
        if len(raw_name) > _MAX_ATTRIBUTE_NAME or len(raw_value) > _MAX_ATTRIBUTE_VALUE:
            raise ValueError("adaptive feature must be bounded")
        cleaned = raw_value.strip()
        if not cleaned:
            continue
        result[raw_name] = cleaned
    return result


def _text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("adaptive feature must be bounded")
    if len(value) > _MAX_TEXT:
        raise ValueError("adaptive feature must be bounded")
    cleaned = value.strip()
    return cleaned or None


def _path(element: HtmlElement) -> list[str]:
    reversed_path: list[str] = []
    current: Any = element
    while current is not None:
        if len(reversed_path) >= _MAX_PATH:
            raise ValueError("adaptive feature must be bounded")
        reversed_path.append(_tag(current.tag))
        current = current.getparent()
    return list(reversed(reversed_path))


def _related(values: Sequence[str]) -> list[str]:
    if len(values) > _MAX_RELATED:
        raise ValueError("adaptive feature must be bounded")
    return [_tag(value) for value in values]


def _encode_feature(feature: Mapping[str, object]) -> bytes:
    encoded = canonical_json({"format": _FORMAT, "feature": feature}).encode("utf-8")
    if len(encoded) > _MAX_FEATURE_BYTES:
        raise ValueError("adaptive feature must be bounded")
    return encoded


def _decode_feature(value: bytes) -> dict[str, object]:
    if len(value) > _MAX_FEATURE_BYTES:
        raise ValueError("adaptive feature must be bounded")
    _validate_json_nesting(value)
    decoded = json.loads(value.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(decoded, dict) or set(decoded) != {"format", "feature"}:
        raise ValueError("adaptive feature must be bounded")
    if decoded["format"] != _FORMAT or not isinstance(decoded["feature"], dict):
        raise ValueError("adaptive feature must be bounded")
    feature = decoded["feature"]
    _validate_feature(feature)
    return feature


def _validate_json_nesting(value: bytes) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == 0x5C:
                escaped = True
            elif character == 0x22:
                in_string = False
            continue
        if character == 0x22:
            in_string = True
        elif character in (0x5B, 0x7B):
            depth += 1
            if depth > 8:
                raise ValueError("adaptive feature must be bounded")
        elif character in (0x5D, 0x7D):
            depth -= 1
            if depth < 0:
                raise ValueError("adaptive feature must be bounded")
    if depth != 0 or in_string:
        raise ValueError("adaptive feature must be bounded")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("adaptive feature must be bounded")
        result[key] = value
    return result


def _validate_feature(feature: Mapping[str, object]) -> None:
    required = {"tag", "attributes", "text", "path"}
    optional = {"parent_name", "parent_attribs", "parent_text", "siblings", "children"}
    if not required <= set(feature) or not set(feature) <= required | optional:
        raise ValueError("adaptive feature must be bounded")
    _tag(feature["tag"])
    if not isinstance(feature["attributes"], dict):
        raise ValueError("adaptive feature must be bounded")
    if _attributes(feature["attributes"]) != feature["attributes"]:
        raise ValueError("adaptive feature must be bounded")
    if _text(feature["text"]) != feature["text"]:
        raise ValueError("adaptive feature must be bounded")
    path = feature["path"]
    if not isinstance(path, list) or not path or len(path) > _MAX_PATH:
        raise ValueError("adaptive feature must be bounded")
    for item in path:
        _tag(item)
    if path[-1] != feature["tag"]:
        raise ValueError("adaptive feature must be bounded")
    parent_keys = {"parent_name", "parent_attribs", "parent_text"}
    if set(feature) & parent_keys and not parent_keys <= set(feature):
        raise ValueError("adaptive feature must be bounded")
    if "parent_name" in feature:
        _tag(feature["parent_name"])
        if not isinstance(feature["parent_attribs"], dict):
            raise ValueError("adaptive feature must be bounded")
        if _attributes(feature["parent_attribs"]) != feature["parent_attribs"]:
            raise ValueError("adaptive feature must be bounded")
        if _text(feature["parent_text"]) != feature["parent_text"]:
            raise ValueError("adaptive feature must be bounded")
    for key in ("siblings", "children"):
        if key in feature:
            values = feature[key]
            if not isinstance(values, list):
                raise ValueError("adaptive feature must be bounded")
            _related(values)
