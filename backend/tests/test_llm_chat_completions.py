"""Tests for the Chat Completions adapter's own spellings.

The protocol x mode matrix lives in `test_llm_protocol.py`. What is here is what
only this protocol has: `choices`, `finish_reason`, a `refusal` field on the
message, and the `max_tokens` / `response_format.json_schema.strict` spellings
that look like the Responses API's and are not.

Every one of these is a silent failure if inherited from the sibling adapter: a
schema nested one level too shallow is accepted and ignored, and a truncation
reported as a transport problem sends an operator after the wrong thing.
"""

from __future__ import annotations

import pytest

from linuxdo_oss.llm import SchemaMode
from linuxdo_oss.llm.chat_completions import (
    ENDPOINT_SUFFIX,
    auth_headers,
    build_payload,
    extract_structured_output,
)
from linuxdo_oss.llm.errors import (
    CATEGORY_INCOMPLETE,
    CATEGORY_INCOMPLETE_MAX_TOKENS,
    CATEGORY_MALFORMED_JSON,
    CATEGORY_NO_OUTPUT_TEXT,
    CATEGORY_PROVIDER_FAILED,
    CATEGORY_REFUSAL,
    ClassifierError,
)

SCHEMA = {"type": "object", "additionalProperties": False, "properties": {}}
API_KEY = "sk-secret-must-never-be-logged-0123456789"


def payload(schema_mode: SchemaMode = SchemaMode.STRICT) -> dict:
    return build_payload(
        model="m",
        system_prompt="SYSTEM",
        user_content="USER",
        json_schema=SCHEMA,
        schema_name="decisions",
        schema_mode=schema_mode,
        max_output_tokens=1024,
    )


def reply(content: str = "{}", *, finish_reason: str = "stop", **message) -> dict:
    return {
        "choices": [{"finish_reason": finish_reason, "message": {"content": content, **message}}]
    }


# ----------------------------------------------------------------------
# The request
# ----------------------------------------------------------------------


def test_the_endpoint_and_auth_header_are_the_openai_style_ones():
    assert ENDPOINT_SUFFIX == "/chat/completions"
    assert auth_headers(API_KEY) == {"Authorization": f"Bearer {API_KEY}"}


def test_the_budget_field_is_max_tokens_not_max_output_tokens():
    """`max_completion_tokens` is what OpenAI now prefers, but this protocol
    exists for third-party coverage and `max_tokens` is what those implement."""
    body = payload()

    assert body["max_tokens"] == 1024
    assert "max_output_tokens" not in body


def test_the_system_prompt_is_a_message_not_a_top_level_field():
    """The opposite of the Anthropic adapter, which rejects a system-role message."""
    body = payload()

    assert body["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert body["messages"][1] == {"role": "user", "content": "USER"}
    assert "system" not in body


def test_the_schema_sits_one_level_deeper_than_in_the_responses_api():
    """`response_format.json_schema.strict`, not `text.format.strict`. Getting the
    nesting wrong produces a request that is accepted and silently unconstrained."""
    fmt = payload()["response_format"]

    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "decisions"
    assert fmt["json_schema"]["schema"] == SCHEMA
    assert fmt["json_schema"]["strict"] is True
    assert "text" not in payload()


def test_json_object_mode_sends_the_bare_type_that_deepseek_and_zhipu_accept():
    assert payload(SchemaMode.JSON_OBJECT)["response_format"] == {"type": "json_object"}


def test_none_mode_omits_the_field_rather_than_emptying_it():
    """Some local runtimes reject `response_format` itself."""
    assert "response_format" not in payload(SchemaMode.NONE)


# ----------------------------------------------------------------------
# The reply
# ----------------------------------------------------------------------


def test_a_well_formed_reply_parses():
    assert extract_structured_output(reply('{"a": 1}'), SchemaMode.STRICT) == {"a": 1}


def test_a_refusal_is_a_refusal_even_when_the_reply_was_also_truncated():
    """Checked before `finish_reason`: reporting a truncated refusal as truncation
    would send an operator after the token budget instead of the prompt."""
    raw = reply("", finish_reason="length", refusal="I cannot help with that")

    with pytest.raises(ClassifierError) as caught:
        extract_structured_output(raw, SchemaMode.STRICT)

    assert caught.value.category == CATEGORY_REFUSAL
    assert caught.value.retryable is False


def test_a_blank_refusal_field_is_not_a_refusal():
    """`refusal: null` is the normal case, and `refusal: ""` is a serializer
    artifact. Treating either as a refusal would fail every healthy reply."""
    assert extract_structured_output(reply('{"a": 1}', refusal=None), SchemaMode.STRICT)
    assert extract_structured_output(reply('{"a": 1}', refusal="   "), SchemaMode.STRICT)


def test_truncation_is_permanent_because_the_same_budget_truncates_again():
    with pytest.raises(ClassifierError) as caught:
        extract_structured_output(reply("{", finish_reason="length"), SchemaMode.STRICT)

    assert caught.value.category == CATEGORY_INCOMPLETE_MAX_TOKENS
    assert caught.value.retryable is False


def test_a_content_filter_stop_is_reported_as_incomplete():
    with pytest.raises(ClassifierError) as caught:
        extract_structured_output(reply("{}", finish_reason="content_filter"), SchemaMode.STRICT)

    assert caught.value.category == CATEGORY_INCOMPLETE
    assert caught.value.retryable is False


def test_an_unknown_finish_reason_does_not_reject_a_valid_reply():
    """The parsed output is the authority. Refusing over an unrecognised
    informational field would reject a compatible endpoint."""
    assert extract_structured_output(reply('{"a": 1}', finish_reason="whatever"), SchemaMode.STRICT)


def test_a_gateway_error_body_returned_with_http_200_is_retryable():
    """Relay gateways report upstream capacity this way instead of with a status."""
    with pytest.raises(ClassifierError) as caught:
        extract_structured_output({"error": {"code": "upstream_busy"}}, SchemaMode.STRICT)

    assert caught.value.category == CATEGORY_PROVIDER_FAILED
    assert caught.value.retryable is True
    assert "upstream_busy" in str(caught.value)


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"choices": []},
        {"choices": "nope"},
        {"choices": ["nope"]},
        {"choices": [{"finish_reason": "stop"}]},
        {"choices": [{"message": "nope"}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": "   "}}]},
    ],
)
def test_a_reply_shaped_like_another_protocol_fails_as_no_output_text(raw):
    """This is what a Responses-API endpoint answers when asked for Chat
    Completions, so the category has to say "wrong shape", not "bad JSON"."""
    with pytest.raises(ClassifierError) as caught:
        extract_structured_output(raw, SchemaMode.STRICT)

    assert caught.value.category == CATEGORY_NO_OUTPUT_TEXT
    assert caught.value.retryable is False


def test_content_that_is_not_json_is_a_permanent_malformed_failure():
    with pytest.raises(ClassifierError) as caught:
        extract_structured_output(reply("<html>502</html>"), SchemaMode.STRICT)

    assert caught.value.category == CATEGORY_MALFORMED_JSON
    assert caught.value.retryable is False
