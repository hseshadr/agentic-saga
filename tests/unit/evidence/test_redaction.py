from copy import deepcopy

import pytest
from pydantic import ValidationError

from agentic_saga.contracts import redaction
from agentic_saga.contracts.redaction import RedactionPolicy, redact_json


def test_should_redact_nested_credentials_without_mutating_input() -> None:
    source = {
        "Authorization": "Bearer reusable-token",
        "nested": [{"api-key": "secret"}, {"credential_ref": "vault://payments/v1"}],
    }
    original = deepcopy(source)

    redacted = redact_json(source, RedactionPolicy())

    assert redacted == {
        "Authorization": "[REDACTED]",
        "nested": [{"api-key": "[REDACTED]"}, {"credential_ref": "vault://payments/v1"}],
    }
    assert source == original


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("cookie", "session=secret"),
        ("Set_Cookie", "session=secret"),
        ("card number", "4242 4242 4242 4242"),
        ("cvv", "123"),
    ],
)
def test_should_normalize_sensitive_key_names(key: str, value: str) -> None:
    assert redact_json({key: value}, RedactionPolicy()) == {key: "[REDACTED]"}


def test_should_redact_bearer_token_outside_sensitive_key() -> None:
    value = {"note": "Bearer reusable-token"}

    assert redact_json(value, RedactionPolicy()) == {"note": "[REDACTED]"}


def test_should_redact_card_number_outside_sensitive_key() -> None:
    value = {"note": "4242 4242 4242 4242"}

    assert redact_json(value, RedactionPolicy()) == {"note": "[REDACTED]"}


def test_should_preserve_mandatory_secret_keys_with_custom_policy() -> None:
    value = {
        "password": "reusable-password",
        "token": "reusable-token",
        "cookie": "session=secret",
        "api-key": "reusable-api-key",
        "cvv": 123,
    }

    redacted = redact_json(value, RedactionPolicy(sensitive_keys=("private-note",)))

    assert isinstance(redacted, dict)
    assert set(redacted.values()) == {"[REDACTED]"}


def test_should_classify_custom_key_even_when_value_is_the_placeholder() -> None:
    policy = RedactionPolicy(sensitive_keys=("email",))

    assert redaction.contains_sensitive_json({"email": "[REDACTED]"}, policy)


def test_should_reject_configurable_redaction_placeholder() -> None:
    with pytest.raises(ValidationError):
        RedactionPolicy.model_validate({"placeholder": "Bearer reusable-secret"})
    copied = RedactionPolicy().model_copy(update={"placeholder": "Bearer reusable-secret"})

    assert copied.placeholder == "[REDACTED]"
    assert redact_json({"note": "Bearer provider-secret"}, copied) == {"note": "[REDACTED]"}


@pytest.mark.parametrize("pan", [4242424242424242, "4242-4242-4242-4242"])
def test_should_redact_numeric_or_string_luhn_pan(pan: int | str) -> None:
    assert redact_json({"public_value": pan}, RedactionPolicy()) == {"public_value": "[REDACTED]"}


@pytest.mark.parametrize(
    "key",
    ["client_secret", "private_key", "session_token", "id_token", "oauth_refresh_token"],
)
def test_should_redact_compound_secret_key_families_with_custom_policy(key: str) -> None:
    policy = RedactionPolicy(sensitive_keys=("private-note",))

    assert redact_json({key: "reusable-value"}, policy) == {key: "[REDACTED]"}


def test_should_preserve_innocuous_secret_substrings() -> None:
    values = {
        "token_count": 2,
        "token_limit": 2_000,
        "cookie_domain": "example.test",
        "password_policy": "minimum-length-12",
        "public_key": "public-material",
        "requires_credentials": False,
    }

    assert redact_json(values, RedactionPolicy()) == values


@pytest.mark.parametrize("key", ["token_count", "token_limit"])
def test_should_redact_non_integer_token_budget_fields(key: str) -> None:
    value = {key: "ordinary-raw-secret"}

    assert redact_json(value, RedactionPolicy()) == {key: "[REDACTED]"}


@pytest.mark.parametrize("key", ["client_secret_ref", "private_key_ref", "session_token_ref"])
def test_should_preserve_only_valid_versioned_secret_references(key: str) -> None:
    valid = "vault://production/credential/v3"

    assert redact_json({key: valid}, RedactionPolicy()) == {key: valid}
    assert redact_json({key: "raw-reference-value"}, RedactionPolicy()) == {key: "[REDACTED]"}


@pytest.mark.parametrize(
    "key",
    [
        "client_secret_value",
        "api-key-value",
        "passwordHash",
        "privateKeyPem",
        "authorization_header",
        "sessionTokenData",
    ],
)
def test_should_redact_sensitive_tokens_followed_by_secret_descriptors(key: str) -> None:
    policy = RedactionPolicy(sensitive_keys=("private-note",))

    assert redact_json({key: "supplied-sensitive-value"}, policy) == {key: "[REDACTED]"}


@pytest.mark.parametrize("key", ["client_secret_value_ref", "apiKeyRawRef"])
def test_should_validate_descriptor_secret_references(key: str) -> None:
    valid = "vault://production/credential/v3"

    assert redact_json({key: valid}, RedactionPolicy()) == {key: valid}
    assert redact_json({key: "raw-reference-value"}, RedactionPolicy()) == {key: "[REDACTED]"}


@pytest.mark.parametrize("key", ["tokenref", "authproofref", "CLIENTSECRETREF"])
def test_should_validate_compact_secret_references(key: str) -> None:
    valid = "vault://production/credential/v3"

    assert redact_json({key: valid}, RedactionPolicy()) == {key: valid}
    assert redact_json({key: "raw-reference-value"}, RedactionPolicy()) == {key: "[REDACTED]"}


def test_should_not_treat_arbitrary_ref_suffix_as_secret_reference() -> None:
    value = {"transferref": "ordinary-public-value"}

    assert redact_json(value, RedactionPolicy()) == value


def test_should_not_exempt_reference_when_ref_is_not_the_final_token() -> None:
    value = {"client_secret_value_ref_content": "raw-reference-value"}

    assert redact_json(value, RedactionPolicy()) == {
        "client_secret_value_ref_content": "[REDACTED]"
    }


@pytest.mark.parametrize(
    "key",
    [
        "CLIENT_SECRET_VALUE",
        "API-KEY-VALUE",
        "PRIVATE_KEY_PEM",
        "PASSWORD_HASH",
        "APIKeyValue",
        "clientSecretVALUE",
    ],
)
def test_should_redact_uppercase_and_mixed_case_secret_descriptors(key: str) -> None:
    policy = RedactionPolicy(sensitive_keys=("private-note",))

    assert redact_json({key: "uppercase-sensitive-value"}, policy) == {key: "[REDACTED]"}


@pytest.mark.parametrize("key", ["CLIENT_SECRET_VALUE_REF", "API-KEY-RAW-REF"])
def test_should_validate_uppercase_descriptor_secret_references(key: str) -> None:
    valid = "vault://production/credential/v4"

    assert redact_json({key: valid}, RedactionPolicy()) == {key: valid}
    assert redact_json({key: "uppercase-raw-reference"}, RedactionPolicy()) == {key: "[REDACTED]"}


def test_should_preserve_case_varied_public_fields() -> None:
    values = {
        "REQUIRES_CREDENTIALS": False,
        "TOKEN_COUNT": 3,
        "PASSWORD-POLICY": "minimum-length-12",
        "PublicKey": "public-material",
    }

    assert redact_json(values, RedactionPolicy()) == values


def test_should_redact_nested_secrets_and_payment_values_together() -> None:
    value = {
        "authorization": "Bearer reusable-token",
        "customer": {"cardNumber": "4242424242424242", "name": "Asha"},
        "items": [{"CVV": "123", "sku": "SHOE-123"}],
    }

    assert redact_json(value, RedactionPolicy()) == {
        "authorization": "[REDACTED]",
        "customer": {"cardNumber": "[REDACTED]", "name": "Asha"},
        "items": [{"CVV": "[REDACTED]", "sku": "SHOE-123"}],
    }
