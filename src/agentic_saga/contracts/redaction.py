"""Leaf contracts and deterministic functions for safe public JSON."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from agentic_saga.contracts.common import JsonScalar, JsonValue, thaw_json

type RedactedJson = JsonScalar | list[RedactedJson] | dict[str, RedactedJson]

_JSON_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_BEARER = re.compile(r"^\s*bearer\s+\S+", re.IGNORECASE)
_CARD = re.compile(r"^(?:\d[ -]?){12,18}\d$")
_KEY_PART = re.compile(r"[A-Za-z0-9]+")
_CAMEL_TOKEN = re.compile(r"[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[0-9]+")
_SECRET_REFERENCE = re.compile(r"^[a-z][a-z0-9+.-]*://[^\s]+/v[1-9][0-9]*$")
_LUHN_REDUCTION = 9
_SECRET_TOKENS = frozenset(
    {
        "authorization",
        "cookie",
        "cvv",
        "cvc",
        "password",
        "passwd",
        "token",
        "secret",
        "credential",
        "credentials",
    }
)
_SECRET_PAIRS = frozenset(
    {
        ("api", "key"),
        ("auth", "key"),
        ("auth", "proof"),
        ("card", "number"),
        ("private", "key"),
        ("security", "code"),
        ("session", "id"),
    }
)
_COMPACT_SECRET_KEYS = frozenset(
    {
        "apikey",
        "authkey",
        "authproof",
        "cardnumber",
        "creditcardnumber",
        "privatekey",
        "proxyauthorization",
        "securitycode",
        "secretkey",
        "sessionid",
        "setcookie",
        "xapikey",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "sessiontoken",
        "clientsecret",
    }
)
_SECRET_DESCRIPTORS = frozenset(
    {"value", "hash", "pem", "header", "data", "material", "content", "raw"}
)
_INTEGER_PUBLIC_KEYS = frozenset({("token", "count"), ("token", "limit")})
_PUBLIC_KEYS = frozenset({("password", "policy"), ("cookie", "domain")})
_REQUIRES_CREDENTIALS = frozenset({("requires", "credential"), ("requires", "credentials")})
_REFERENCE_BASES = _SECRET_TOKENS | _COMPACT_SECRET_KEYS


class RedactionPolicy(BaseModel):
    """Immutable recursive credential-redaction policy."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    sensitive_keys: tuple[str, ...] = (
        "authorization",
        "proxyauthorization",
        "apikey",
        "xapikey",
        "cookie",
        "setcookie",
        "cardnumber",
        "creditcardnumber",
        "cvv",
        "cvc",
        "securitycode",
        "authproof",
    )

    @property
    def placeholder(self) -> Literal["[REDACTED]"]:
        """Return the one non-configurable public sentinel."""
        return "[REDACTED]"


def _normalized(key: str) -> str:
    return "".join(character for character in key.casefold() if character.isalnum())


def _redact_mapping(value: dict[str, RedactedJson], policy: RedactionPolicy) -> RedactedJson:
    custom = frozenset(_normalized(key) for key in policy.sensitive_keys)
    return {key: _redact_member(key, item, policy, custom) for key, item in value.items()}


def _redact_member(
    key: str, value: RedactedJson, policy: RedactionPolicy, custom: frozenset[str]
) -> RedactedJson:
    if _is_sensitive_member(key, value, custom):
        return policy.placeholder
    return _redact(value, policy)


def _is_sensitive_member(key: str, value: object, custom: frozenset[str]) -> bool:
    if _normalized(key) in custom or _is_mandatory_secret_key(key, value):
        return True
    return _is_secret_reference_key(key) and not _is_versioned_secret_reference(value)


def _key_tokens(key: str) -> tuple[str, ...]:
    return tuple(token for part in _KEY_PART.findall(key) for token in _part_tokens(part))


def _part_tokens(part: str) -> tuple[str, ...]:
    if part.isupper():
        return (part.casefold(),)
    return tuple(match.group(0).casefold() for match in _CAMEL_TOKEN.finditer(part))


def _secret_boundary(tokens: tuple[str, ...]) -> int | None:
    for index in range(len(tokens) - 1, -1, -1):
        if _is_secret_token(tokens, index):
            return index
    return None


def _is_secret_token(tokens: tuple[str, ...], index: int) -> bool:
    token = tokens[index]
    if token in _SECRET_TOKENS or token in _COMPACT_SECRET_KEYS:
        return True
    if index == 0:
        return False
    return (tokens[index - 1], token) in _SECRET_PAIRS


def _is_public_usage(tokens: tuple[str, ...], value: object) -> bool:
    if tokens in _REQUIRES_CREDENTIALS:
        return type(value) is bool
    if tokens in _INTEGER_PUBLIC_KEYS:
        return type(value) is int
    return tokens in _PUBLIC_KEYS


def _is_reference_usage(tokens: tuple[str, ...], boundary: int) -> bool:
    trailing = tokens[boundary + 1 :]
    return (
        bool(trailing)
        and trailing[-1] == "ref"
        and all(token in _SECRET_DESCRIPTORS for token in trailing[:-1])
    )


def _is_mandatory_secret_key(key: str, value: object) -> bool:
    tokens = _key_tokens(key)
    boundary = _secret_boundary(tokens)
    if boundary is None or _is_public_usage(tokens, value):
        return False
    return not _is_reference_usage(tokens, boundary)


def _is_secret_reference_key(key: str) -> bool:
    tokens = _key_tokens(key)
    boundary = _secret_boundary(tokens)
    if boundary is not None:
        return _is_reference_usage(tokens, boundary)
    normalized = _normalized(key)
    return normalized.endswith("ref") and normalized[:-3] in _REFERENCE_BASES


def _is_versioned_secret_reference(value: object) -> bool:
    return isinstance(value, str) and _SECRET_REFERENCE.fullmatch(value) is not None


def _redact(value: RedactedJson, policy: RedactionPolicy) -> RedactedJson:
    if isinstance(value, dict):
        return _redact_mapping(value, policy)
    if isinstance(value, list):
        return [_redact(item, policy) for item in value]
    return _redact_scalar(value, policy)


def _contains_sensitive(
    value: RedactedJson, policy: RedactionPolicy, custom: frozenset[str]
) -> bool:
    if isinstance(value, dict):
        return _contains_sensitive_mapping(value, policy, custom)
    if isinstance(value, list):
        return any(_contains_sensitive(item, policy, custom) for item in value)
    return _redact_scalar(value, policy) != value


def _contains_sensitive_mapping(
    value: dict[str, RedactedJson], policy: RedactionPolicy, custom: frozenset[str]
) -> bool:
    return any(
        _is_sensitive_member(key, item, custom) or _contains_sensitive(item, policy, custom)
        for key, item in value.items()
    )


def _redact_scalar(value: JsonScalar, policy: RedactionPolicy) -> JsonScalar:
    if isinstance(value, str):
        return _redact_string(value, policy)
    if _is_integer_card(value):
        return policy.placeholder
    return value


def _is_integer_card(value: JsonScalar) -> bool:
    return type(value) is int and _is_card_number(str(value))


def _redact_string(value: str, policy: RedactionPolicy) -> str:
    return policy.placeholder if _is_sensitive_string(value) else value


def _is_sensitive_string(value: str) -> bool:
    return _BEARER.match(value) is not None or _is_card_number(value)


def _is_card_number(value: str) -> bool:
    if _CARD.fullmatch(value) is None:
        return False
    digits = tuple(int(character) for character in value if character.isdigit())
    checksum = sum(_luhn_digit(digit, index, len(digits)) for index, digit in enumerate(digits))
    return checksum % 10 == 0


def _luhn_digit(digit: int, index: int, length: int) -> int:
    if (length - index) % 2:
        return digit
    doubled = digit * 2
    return doubled - _LUHN_REDUCTION if doubled > _LUHN_REDUCTION else doubled


def redact_json(value: object, policy: RedactionPolicy) -> RedactedJson:
    """Return a detached recursively redacted strict-JSON value."""
    validated = _JSON_ADAPTER.validate_python(value)
    return _redact(thaw_json(validated), policy)


def contains_sensitive_json(value: object, policy: RedactionPolicy) -> bool:
    """Return whether strict JSON contains material classified by ``policy``."""
    validated = _JSON_ADAPTER.validate_python(value)
    custom = frozenset(_normalized(key) for key in policy.sensitive_keys)
    return _contains_sensitive(thaw_json(validated), policy, custom)


__all__ = ["RedactionPolicy", "contains_sensitive_json", "redact_json"]
