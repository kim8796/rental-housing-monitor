from __future__ import annotations

from personal_monitor.security.credential_names import SENSITIVE_CREDENTIAL_NAMES


def _base_variants(name: str) -> set[str]:
    parts = name.split("_")
    variants = {
        name,
        name.upper(),
        name.capitalize(),
    }
    if len(parts) > 1:
        compact = "".join(parts)
        variants.update(
            {
                "-".join(parts),
                "-".join(parts).upper(),
                compact,
                compact.upper(),
                parts[0] + "".join(part.title() for part in parts[1:]),
                "".join(part.title() for part in parts),
            }
        )
    return variants


def _key_variants() -> tuple[str, ...]:
    variants: set[str] = set()
    for name in SENSITIVE_CREDENTIAL_NAMES:
        base = _base_variants(name)
        variants.update(base)
        variants.update(f" {value} " for value in base)
        variants.update(f"{quote}{name}{quote}" for quote in "\"'`")
        variants.update(f" {quote} {name} {quote} " for quote in "\"'`")
    return tuple(sorted(variants, key=lambda value: (value.casefold(), value)))


SENSITIVE_KEY_VARIANTS = _key_variants()
SENSITIVE_ASSIGNMENTS = tuple(f"{name} = supersecretvalue" for name in SENSITIVE_KEY_VARIANTS)

EXACT_BOUNDARY_ASSIGNMENTS = (
    "AccessToken=supersecretvalue",
    "ClientSecret=supersecretvalue",
    "SetCookie=supersecretvalue",
    '"authorization"=supersecretvalue',
    "'authorization'=supersecretvalue",
    "`authorization`=supersecretvalue",
    ' " authorization " = supersecretvalue',
    "ACCESS_TOKEN=supersecretvalue",
    "access-token=supersecretvalue",
)

BENIGN_CREDENTIAL_LIKE_KEYS = (
    "monkey",
    "hockey",
    "session_state",
    "authorization_code",
    '"authorization',
    "authorization'",
    "(authorization)",
    "x-authorization",
    "api.key",
)
