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
PUNCTUATED_ASSIGNMENTS = (
    'config.authorization="supersecretvalue"',
    "https://example.com/?authorization=supersecretvalue",
    "?access_token=supersecretvalue",
    "/password=supersecretvalue",
    *(f"prefix{punctuation}authorization=supersecretvalue" for punctuation in ".?/&#:"),
)


def _compound_field_variants() -> tuple[str, ...]:
    variants: set[str] = set()
    for name in SENSITIVE_CREDENTIAL_NAMES:
        parts = name.split("_")
        snake = "_".join(parts)
        kebab = "-".join(parts)
        pascal = "".join(part.title() for part in parts)
        variants.update(
            {
                f"user_{snake}_value",
                f"user-{kebab}-value",
                f"user{pascal}Value",
                f"User{pascal}Value",
            }
        )
    variants.update(
        {
            "user_password",
            "api_key_value",
            "session_token",
            "payment_signature",
            "userPassword",
            "api-key-value",
            "sessionToken",
            "payment-signature",
        }
    )
    return tuple(sorted(variants, key=lambda value: (value.casefold(), value)))


SENSITIVE_COMPOUND_FIELD_NAMES = _compound_field_variants()

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

BENIGN_ASSIGNMENT_KEYS = (
    "myauthorization",
    "monkey",
    "hockey",
    "session_state",
    "authorization_code",
    '"authorization',
    "authorization'",
    "(authorization)",
    "x-authorization",
)

BENIGN_COMPOUND_FIELD_NAMES = (
    "monkey",
    "hockey",
    "title",
    "price",
    "product_url",
    "user_name",
    "apiKeynote",
    "sessionstate",
)
