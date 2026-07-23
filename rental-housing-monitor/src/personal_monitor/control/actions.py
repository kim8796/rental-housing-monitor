from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Final

from personal_monitor.storage.schema import canonical_json, transaction

_TOKEN_BYTES: Final = 24
_TOKEN_URLSAFE: Final = secrets.token_urlsafe
_TOKEN_LENGTH: Final = 32
_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9_-]{32}\Z")
_OWNER_RE: Final = re.compile(r"telegram-user:[1-9][0-9]{0,18}\Z")
_ACTIONS: Final = frozenset(
    {
        "create",
        "delete",
        "pause",
        "repair_activation",
        "resume",
        "schedule_change",
        "update",
    }
)
_MAX_PAYLOAD_BYTES: Final = 65_536
_MAX_JSON_DEPTH: Final = 16
_MAX_JSON_NODES: Final = 1_024
_MAX_JSON_KEY_CHARS: Final = 128
_MAX_JSON_STRING_CHARS: Final = 4_096
_MAX_JSON_STRING_BYTES: Final = 16_384
_MAX_INTEGER: Final = 2**63 - 1


class ActionDenied(RuntimeError):
    """Fixed, non-oracular failure at the one-time action boundary."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "ActionDenied(<redacted>)"


class _RejectAction(Exception):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class PendingAction:
    token: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if _valid_token(self.token) is None or _utc_datetime(self.expires_at) is None:
            raise ValueError("invalid pending action")

    @property
    def confirm_callback(self) -> str:
        return f"confirm:{self.token}"

    @property
    def cancel_callback(self) -> str:
        return f"cancel:{self.token}"

    def __repr__(self) -> str:
        return "<PendingAction redacted>"


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ConsumedAction:
    action: str
    payload: Mapping[str, object]
    owner_id: str
    operation: str = "confirm"
    _issuer: object | None = field(default=None, init=False, repr=False)
    _receipt: object | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            _valid_action(self.action) is None
            or _valid_owner(self.owner_id) is None
            or self.operation not in {"confirm", "edit"}
        ):
            raise ValueError("invalid consumed action")
        try:
            _, frozen = _validated_payload(self.payload)
        except Exception:
            raise ValueError("invalid consumed action") from None
        object.__setattr__(self, "payload", frozen)

    def __repr__(self) -> str:
        return "<ConsumedAction redacted>"


class PendingActionService:
    __slots__ = (
        "_connection",
        "_connection_anchor",
        "_issued",
        "_issued_anchor",
        "_issuer",
        "_issuer_anchor",
        "_token_source",
        "_token_source_anchor",
    )

    def __init__(self, connection: sqlite3.Connection) -> None:
        if type(connection) is not sqlite3.Connection:
            raise ValueError("invalid pending action storage")
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_connection_anchor", connection)
        issuer = object()
        issued: dict[object, tuple[ConsumedAction, str, str, str, str]] = {}
        object.__setattr__(self, "_issuer", issuer)
        object.__setattr__(self, "_issuer_anchor", issuer)
        object.__setattr__(self, "_issued", issued)
        object.__setattr__(self, "_issued_anchor", issued)
        object.__setattr__(self, "_token_source", _TOKEN_URLSAFE)
        object.__setattr__(self, "_token_source_anchor", _TOKEN_URLSAFE)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("PendingActionService composition is sealed")

    def __repr__(self) -> str:
        return "<PendingActionService redacted>"

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection_anchor

    def create(
        self,
        owner_id: str,
        action: str,
        payload: Mapping[str, object],
        *,
        now: datetime,
    ) -> PendingAction:
        owner = _valid_owner(owner_id)
        action_name = _valid_action(action)
        normalized_now = _utc_datetime(now)
        try:
            payload_json, _ = _validated_payload(payload)
        except Exception:
            payload_json = None
        if (
            not self._integrity_ok()
            or owner is None
            or action_name is None
            or normalized_now is None
            or payload_json is None
        ):
            raise ValueError("invalid pending action")

        token = self._token_source_anchor(_TOKEN_BYTES)
        if _valid_token(token) is None:
            raise ValueError("invalid pending action")
        try:
            expires_at = normalized_now + timedelta(minutes=10)
        except (OverflowError, ValueError):
            raise ValueError("invalid pending action") from None
        token_hash = _token_hash(token)
        failed = False
        try:
            with transaction(self._connection_anchor):
                self._connection_anchor.execute(
                    "INSERT INTO pending_actions("
                    "token_hash, owner_id, action, payload_json, expires_at, consumed_at"
                    ") VALUES (?, ?, ?, ?, ?, NULL)",
                    (
                        token_hash,
                        owner,
                        action_name,
                        payload_json,
                        expires_at.isoformat(),
                    ),
                )
        except Exception:
            failed = True
        if failed:
            raise ValueError("invalid pending action") from None
        return PendingAction(token, expires_at)

    def consume(
        self,
        token: str,
        owner_id: str,
        *,
        now: datetime,
        operation: str = "confirm",
    ) -> ConsumedAction:
        safe_token = _valid_token(token)
        owner = _valid_owner(owner_id)
        normalized_now = _utc_datetime(now)
        if (
            not self._integrity_ok()
            or safe_token is None
            or owner is None
            or normalized_now is None
            or operation not in {"confirm", "edit"}
            or self._connection_anchor.in_transaction
        ):
            raise ActionDenied("pending action denied")

        result: ConsumedAction | None = None
        failed = False
        try:
            with transaction(self._connection_anchor, immediate=True):
                row = self._connection_anchor.execute(
                    "SELECT owner_id, action, payload_json, expires_at, consumed_at "
                    "FROM pending_actions WHERE token_hash = ?",
                    (_token_hash(safe_token),),
                ).fetchone()
                if row is None:
                    raise _RejectAction
                expires_at = _stored_expiry(row["expires_at"])
                action = _valid_action(row["action"])
                payload_json, payload = _stored_payload(row["payload_json"])
                if (
                    row["owner_id"] != owner
                    or row["consumed_at"] is not None
                    or expires_at is None
                    or normalized_now >= expires_at
                    or action is None
                    or payload_json is None
                    or payload is None
                ):
                    raise _RejectAction
                cursor = self._connection_anchor.execute(
                    "UPDATE pending_actions SET consumed_at = ? "
                    "WHERE token_hash = ? AND owner_id = ? AND consumed_at IS NULL",
                    (normalized_now.isoformat(), _token_hash(safe_token), owner),
                )
                if cursor.rowcount != 1:
                    raise _RejectAction
                receipt = object()
                result = ConsumedAction(action, payload, owner, operation)
                object.__setattr__(result, "_issuer", self._issuer_anchor)
                object.__setattr__(result, "_receipt", receipt)
                self._issued_anchor[receipt] = (
                    result,
                    action,
                    payload_json,
                    owner,
                    operation,
                )
        except Exception:
            failed = True
        if failed and result is not None and result._receipt is not None:
            self._issued_anchor.pop(result._receipt, None)
        if failed or result is None:
            raise ActionDenied("pending action denied") from None
        return result

    def claim(self, action: object) -> bool:
        """Claim one exact issuer-bound receipt before any control mutation."""
        if not self._integrity_ok() or type(action) is not ConsumedAction:
            return False
        try:
            receipt = action._receipt
            if action._issuer is not self._issuer_anchor or receipt is None:
                return False
            record = self._issued_anchor.pop(receipt, None)
            if record is None:
                return False
            issued, expected_action, expected_payload, expected_owner, expected_operation = record
            payload_json, _ = _validated_payload(action.payload)
            return (
                issued is action
                and action.action == expected_action
                and payload_json == expected_payload
                and action.owner_id == expected_owner
                and action.operation == expected_operation
            )
        except Exception:
            return False

    def discard(self, action: object) -> bool:
        """Consume an unused in-memory receipt, such as a cancelled callback."""
        return self.claim(action)

    def revoke(self, token: str, owner_id: str) -> None:
        safe_token = _valid_token(token)
        owner = _valid_owner(owner_id)
        if (
            not self._integrity_ok()
            or safe_token is None
            or owner is None
            or self._connection_anchor.in_transaction
        ):
            raise ValueError("invalid pending action")
        try:
            with transaction(self._connection_anchor, immediate=True):
                self._connection_anchor.execute(
                    "DELETE FROM pending_actions WHERE token_hash = ? AND owner_id = ? "
                    "AND consumed_at IS NULL",
                    (_token_hash(safe_token), owner),
                )
        except Exception:
            raise ValueError("invalid pending action") from None

    def _integrity_ok(self) -> bool:
        try:
            return (
                type(self._connection) is sqlite3.Connection
                and self._connection is self._connection_anchor
                and self._issuer is self._issuer_anchor
                and type(self._issued) is dict
                and self._issued is self._issued_anchor
                and self._token_source is self._token_source_anchor
                and self._token_source_anchor is _TOKEN_URLSAFE
            )
        except Exception:
            return False


def _valid_owner(value: object) -> str | None:
    if type(value) is not str or _OWNER_RE.fullmatch(value) is None:
        return None
    return value


def _valid_action(value: object) -> str | None:
    if type(value) is not str or value not in _ACTIONS:
        return None
    return value


def _valid_token(value: object) -> str | None:
    if type(value) is not str or len(value) != _TOKEN_LENGTH or _TOKEN_RE.fullmatch(value) is None:
        return None
    return value


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _utc_datetime(value: object) -> datetime | None:
    if type(value) is not datetime:
        return None
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except Exception:
        return None


def _stored_expiry(value: object) -> datetime | None:
    if type(value) is not str or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (OverflowError, ValueError):
        return None
    normalized = _utc_datetime(parsed)
    if normalized is None or normalized.isoformat() != value:
        return None
    return normalized


def _stored_payload(value: object) -> tuple[str | None, Mapping[str, object] | None]:
    if type(value) is not str:
        return None, None
    try:
        encoded = value.encode("utf-8", errors="strict")
        if len(encoded) > _MAX_PAYLOAD_BYTES or not _json_nesting_is_safe(value):
            return None, None
        decoded = json.loads(value, parse_constant=_reject_json_constant)
        canonical, frozen = _validated_payload(decoded)
    except (UnicodeError, TypeError, ValueError, OverflowError, RecursionError):
        return None, None
    if canonical != value:
        return None, None
    return canonical, frozen


def _validated_payload(value: object) -> tuple[str, Mapping[str, object]]:
    if not isinstance(value, Mapping):
        raise TypeError
    counter = [0]
    validated = _validate_json_value(value, depth=1, counter=counter)
    if not isinstance(validated, Mapping):
        raise TypeError
    payload_json = canonical_json(validated)
    if len(payload_json.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise ValueError
    frozen = _freeze_json(validated)
    if not isinstance(frozen, Mapping):
        raise TypeError
    return payload_json, frozen


def _validate_json_value(value: object, *, depth: int, counter: list[int]) -> object:
    counter[0] += 1
    if counter[0] > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
        raise ValueError
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str or not _valid_json_string(key, max_chars=_MAX_JSON_KEY_CHARS):
                raise ValueError
            result[key] = _validate_json_value(item, depth=depth + 1, counter=counter)
        return result
    if isinstance(value, (list, tuple)):
        return [_validate_json_value(item, depth=depth + 1, counter=counter) for item in value]
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not -_MAX_INTEGER <= value <= _MAX_INTEGER:
            raise ValueError
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError
        return value
    if type(value) is str and _valid_json_string(value, max_chars=_MAX_JSON_STRING_CHARS):
        return value
    raise ValueError


def _valid_json_string(value: str, *, max_chars: int) -> bool:
    if len(value) > max_chars:
        return False
    try:
        if len(value.encode("utf-8", errors="strict")) > _MAX_JSON_STRING_BYTES:
            return False
    except UnicodeEncodeError:
        return False
    return not any(unicodedata.category(character).startswith("C") for character in value)


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _json_nesting_is_safe(value: str) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                return False
        elif character in "]}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string


def _reject_json_constant(_value: str) -> object:
    raise ValueError
