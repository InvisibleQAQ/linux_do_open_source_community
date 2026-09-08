"""Tests for the Anthropic Messages adapter's three departures from OpenAI shape.

Each one is a silent failure if inherited from either sibling adapter:

1. `x-api-key` + `anthropic-version`, not `Authorization: Bearer` — a Bearer
   header here is simply not authenticated.
2. A top-level `system` string, not a `role: "system"` message — a system-role
   message is rejected outright.
3. Structured output as a FORCED tool call, not a response format, and it takes
   TWO fields. `tool_choice` stops the model answering in prose; the tool
   definition's own top-level `strict: true` is what gates Anthropic's grammar
   constraint, so without it the `canonical_url` enum would be best effort about
   types and required fields. Both are asserted, including the placement — a
   `strict` key inside `tool_choice` would be silently ignored and look fine.
"""

from __future__ import annotations

import pytest

from linuxdo_oss.llm import SchemaMode
from linuxdo_oss.llm.anthropic import (
    ANTHROPIC_VERSION,
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
API_KEY = "sk-ant-secret-must-never-be-logged-0123456789"


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


def reply(*blocks, stop_reason: str = "tool_use") -> dict:
    return {"type": "message", "stop_reason": stop_reason, "content": list(blocks)}


TOOL_USE = {"type": "tool_use", "id": "tu_1", "name": "decisions", "input": {"a": 1}}


# ----------------------------------------------------------------------
# The request
# ----------------------------------------------------------------------


def test_the_version_lives_in_the_path_so_a_v1_root_is_a_paste_o():
    """The root is `https://api.anthropic.com` with NO `/v1` — the opposite of the
    OpenAI convention. This adapter declares nothing about it: `/v1` is a path
    prefix of ENDPOINT_SUFFIX, so `protocol.resolve_base_url` strips it back from
    that fact alone. See test_llm_protocol.py for the behaviour."""
    assert ENDPOINT_SUFFIX == "/v1/messages"


def test_auth_is_x_api_key_plus_a_pinned_version_never_bearer():
    headers = auth_headers(API_KEY)

    assert headers == {"x-api-key": API_KEY, "anthropic-version": ANTHROPIC_VERSION}
    assert "Authorization" not in headers


def test_the_system_prompt_is_a_top_level_field_not_a_message():
    body = payload()

    assert body["system"] == "SYSTEM"
    assert body["messages"] == [{"role": "user", "content": "USER"}]
    assert all(message["role"] != "system" for message in body["messages"])


def test_max_tokens_is_required_by_this_api():
    body = payload()

    assert body["max_tokens"] == 1024
    assert "max_output_tokens" not in body


def test_strict_mode_forces_one_named_tool():
    """`input_schema` is only binding while `tool_choice` names the tool."""
    body = payload()

    assert body["tools"][0]["name"] == "decisions"
    assert body["tools"][0]["input_schema"] == SCHEMA
    assert body["tool_choice"] == {"type": "tool", "name": "decisions"}
    # This API has no response_format; the schema travels as the tool only.
    assert "response_format" not in body
    assert "text" not in body


def test_strict_mode_sends_the_tool_level_strict_flag():
    """The flag that turns the forced tool call into a grammar rather than a
    strong request. Without it Anthropic documents types and required fields as
    best effort, which would make `SchemaMode.STRICT` mean less here than under
    the other two protocols."""
    tool = payload()["tools"][0]

    assert tool["strict"] is True
    # Placement is the part that can silently regress: `strict` is a TOP-LEVEL
    # field on the tool definition, beside the other three. Inside `tool_choice`
    # it would be accepted, ignored, and leave the enum unenforced.
    assert set(tool) == {"name", "description", "input_schema", "strict"}
    assert "strict" not in payload()["tool_choice"]


@pytest.mark.parametrize("schema_mode", [SchemaMode.JSON_OBJECT, SchemaMode.NONE])
def test_both_degraded_modes_send_a_plain_message(schema_mode):
    """This API has no response-format field, so the two modes collapse into one
    request here. They stay distinct in configuration because they do not collapse
    under the other two protocols."""
    body = payload(schema_mode)

    assert "tools" not in body
    assert "tool_choice" not in body
    # No `tools` means no place for the tool-level flag either; it must not leak
    # to the top level of the request as a consolation prize.
    assert "strict" not in body
    assert body["system"] == "SYSTEM"


# ----------------------------------------------------------------------
# The reply
# ----------------------------------------------------------------------


def test_the_tool_input_is_taken_as_an_already_parsed_object():
    """No round-trip through a JSON string: `input` arrives as an object."""
    assert extract_structured_output(reply(TOOL_USE), SchemaMode.STRICT) == {"a": 1}


def test_the_tool_block_is_found_after_leading_text_or_thinking_blocks():
    """Never by index — a reasoning model puts other blocks in front, exactly as
    it does under the Responses API."""
    raw = reply({"type": "thinking", "thinking": "..."}, {"type": "text", "text": "sure"}, TOOL_USE)

    assert extract_structured_output(raw, SchemaMode.STRICT) == {"a": 1}


def test_a_reply_with_no_tool_block_says_tool_choice_was_ignored():
    """The diagnostic that matters: the endpoint accepted `tool_choice` and did
    not honour it, which is a compatibility fact, not a bad-JSON problem."""
    with pytest.raises(ClassifierError) as caught:
        extract_structured_output(reply({"type": "text", "text": "{}"}), SchemaMode.STRICT)

    assert caught.value.category == CATEGORY_NO_OUTPUT_TEXT
    assert "tool_choice" in str(caught.value)


def test_a_tool_block_carrying_a_non_object_input_is_refused():
    raw = reply({"type": "tool_use", "name": "decisions", "input": "not an object"})

    with pytest.raises(ClassifierError) as caught:
        extract_structured_output(raw, SchemaMode.STRICT)

    assert caught.value.category == CATEGORY_NO_OUTPUT_TEXT


def test_a_degraded_reply_reads_the_text_block():
    raw = reply({"type": "text", "text": '{"a": 1}'}, stop_reason="end_turn")

    assert extract_structured_output(raw, SchemaMode.JSON_OBJECT) == {"a": 1}


def test_a_degraded_reply_with_no_text_block_is_refused():
    raw = reply({"type": "thinking", "thinking": "..."}, stop_reason="end_turn")

    with pytest.raises(ClassifierError) as caught:
        extract_structured_output(raw, SchemaMode.JSON_OBJECT)

    assert caught.value.category == CATEGORY_NO_OUTPUT_TEXT


def test_a_degraded_reply_whose_text_is_not_json_is_malformed():
    raw = reply({"type": "text", "text": "抱歉，我帮不了。"}, stop_reason="end_turn")

    with pytest.raises(ClassifierError) as caught:
        extract_structured_output(raw, SchemaMode.JSON_OBJECT)

    assert caught.value.category == CATEGORY_MALFORMED_JSON


def test_a_refusal_stop_reason_is_a_refusal():
    with pytest.raises(ClassifierError) as caught:
        extract_structured_output(reply(TOOL_USE, stop_reason="refusal"), SchemaMode.STRICT)

    assert caught.value.category == CATEGORY_REFUSAL
    assert caught.value.retryable is False


def test_truncation_is_permanent_because_the_same_budget_truncates_again():
    with pytest.raises(ClassifierError) as caught:
        extract_structured_output(reply(TOOL_USE, stop_reason="max_tokens"), SchemaMode.STRICT)

    assert caught.value.category == CATEGORY_INCOMPLETE_MAX_TOKENS
    assert caught.value.retryable is False


def test_an_overflowing_context_window_is_reported_as_truncation():
    """A documented stop_reason, and a different failure from `max_tokens`.

    The INPUT filled the window, so raising the output budget cannot help. Before
    this was named it fell through to `_tool_use_input` and got reported as "the
    endpoint ignored tool_choice", sending an operator after the wrong thing.
    """
    raw = reply(TOOL_USE, stop_reason="model_context_window_exceeded")

    with pytest.raises(ClassifierError) as caught:
        extract_structured_output(raw, SchemaMode.STRICT)

    assert caught.value.category == CATEGORY_INCOMPLETE
    assert caught.value.retryable is False


def test_an_error_envelope_returned_with_http_200_is_retryable():
    """Some proxies answer 200 with `{"type": "error", ...}`."""
    raw = {"type": "error", "error": {"type": "overloaded_error", "message": "..."}}

    with pytest.raises(ClassifierError) as caught:
        extract_structured_output(raw, SchemaMode.STRICT)

    assert caught.value.category == CATEGORY_PROVIDER_FAILED
    assert caught.value.retryable is True
    assert "overloaded_error" in str(caught.value)


@pytest.mark.parametrize("raw", [{}, {"content": "nope"}, {"content": None}])
def test_a_reply_shaped_like_another_protocol_fails_as_no_output_text(raw):
    with pytest.raises(ClassifierError) as caught:
        extract_structured_output(raw, SchemaMode.STRICT)

    assert caught.value.category == CATEGORY_NO_OUTPUT_TEXT
