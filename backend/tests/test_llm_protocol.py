"""Tests for dispatch, the adapter contract, and the protocol x mode matrix.

Three protocols times three Schema Modes is nine request shapes, and the failure
mode of a matrix like this is not a crash — it is one cell quietly producing a
body the endpoint accepts and ignores. So the matrix is enumerated here from the
enums themselves: adding a protocol or a mode without covering it makes these
tests fail, rather than leaving a hole nobody notices.

The single most important test in this file is
`test_the_allowlist_gate_holds_under_every_protocol_and_mode`. Everything else in
the package can be wrong and the database survives; that one is the guarantee.
"""

from __future__ import annotations

import json

import pytest

from linuxdo_oss.classifier import (
    CATEGORY_MALFORMED_JSON,
    CATEGORY_UNKNOWN_REPOSITORY,
    SCHEMA_NAME,
    CandidateRepository,
    ClassifierError,
    build_json_schema,
    build_payload,
    parse_response,
)
from linuxdo_oss.llm import (
    BASE_URL_FORBIDDEN_SUFFIXES,
    LLMProtocol,
    SchemaMode,
    WireAdapter,
    endpoint_url,
    get_adapter,
    validate_base_url,
)

CANDIDATE = CandidateRepository(
    canonical_url="https://github.com/owner/tool",
    owner="owner",
    repo="tool",
    evidence_url="https://github.com/owner/tool/issues/12",
    post_number=1,
)
CANDIDATES = (CANDIDATE,)
ALLOWED = {CANDIDATE.canonical_url}

DECISIONS = {
    "decisions": [
        {
            "canonical_url": CANDIDATE.canonical_url,
            "decision": "include",
            "display_name": "tool",
            "summary": "一个命令行小工具。",
            "evidence_excerpt": "分享一个开源小工具",
            "confidence": 0.9,
        }
    ]
}

FABRICATED = {
    "decisions": [
        {
            "canonical_url": "https://github.com/never/linked",
            "decision": "include",
            "display_name": "linked",
            "summary": "模型编造的仓库。",
            "evidence_excerpt": None,
            "confidence": None,
        }
    ]
}

# Where each protocol carries the model's structured output, so a reply can be
# faked for any of them from one JSON string. Keeping this in the tests rather
# than importing a helper from the package is deliberate: a shared builder that
# drifted with the adapter would hide exactly the bug these tests exist to catch.
BASE_URLS = {
    LLMProtocol.RESPONSES: "https://api.example.com/v1",
    LLMProtocol.CHAT_COMPLETIONS: "https://api.example.com/v1",
    LLMProtocol.ANTHROPIC: "https://api.anthropic.com",
}


def reply_for(protocol: LLMProtocol, schema_mode: SchemaMode, text: str) -> dict:
    if protocol is LLMProtocol.RESPONSES:
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        }

    if protocol is LLMProtocol.CHAT_COMPLETIONS:
        return {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}

    if schema_mode is SchemaMode.STRICT:
        return {
            "type": "message",
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "name": SCHEMA_NAME, "input": json.loads(text)}],
        }

    return {
        "type": "message",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
    }


MATRIX = [(p, m) for p in LLMProtocol for m in SchemaMode]


# ----------------------------------------------------------------------
# Dispatch and the adapter contract
# ----------------------------------------------------------------------


@pytest.mark.parametrize("protocol", list(LLMProtocol))
def test_every_protocol_resolves_to_an_adapter_satisfying_the_contract(protocol):
    adapter = get_adapter(protocol)

    assert isinstance(adapter, WireAdapter)
    assert adapter.ENDPOINT_SUFFIX.startswith("/")
    assert isinstance(adapter.FORBIDDEN_BASE_SUFFIXES, tuple)


def test_each_protocol_gets_a_distinct_adapter():
    """A dispatch that fell through to the same module would be invisible
    otherwise: two protocols would happily build the same wrong body."""
    modules = {get_adapter(p).__name__ for p in LLMProtocol}

    assert len(modules) == len(LLMProtocol)


@pytest.mark.parametrize(
    ("protocol", "expected"),
    [
        (LLMProtocol.RESPONSES, "https://api.example.com/v1/responses"),
        (LLMProtocol.CHAT_COMPLETIONS, "https://api.example.com/v1/chat/completions"),
        (LLMProtocol.ANTHROPIC, "https://api.anthropic.com/v1/messages"),
    ],
)
def test_the_endpoint_url_is_the_root_plus_the_protocol_suffix(protocol, expected):
    root = (
        "https://api.anthropic.com"
        if protocol is LLMProtocol.ANTHROPIC
        else "https://api.example.com/v1"
    )

    assert endpoint_url(protocol, root) == expected
    assert endpoint_url(protocol, root + "///") == expected


def test_the_enum_values_are_the_spellings_configuration_uses():
    """`StrEnum`, so a member renders as its configured spelling in a log line."""
    assert f"{LLMProtocol.CHAT_COMPLETIONS}" == "chat_completions"
    assert f"{SchemaMode.JSON_OBJECT}" == "json_object"


# ----------------------------------------------------------------------
# Base URL validation
# ----------------------------------------------------------------------


@pytest.mark.parametrize("protocol", list(LLMProtocol))
@pytest.mark.parametrize("suffix", BASE_URL_FORBIDDEN_SUFFIXES)
def test_a_pasted_endpoint_path_is_rejected_for_every_protocol(protocol, suffix):
    """These endings are wrong everywhere, not only under the protocol that owns
    them: the suffix is always the adapter's to append."""
    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        validate_base_url(protocol, f"https://api.example.com/v1{suffix}")


def test_only_anthropic_rejects_a_v1_root():
    """The asymmetry is the whole reason this check is per-protocol."""
    validate_base_url(LLMProtocol.RESPONSES, "https://api.openai.com/v1")
    validate_base_url(LLMProtocol.CHAT_COMPLETIONS, "https://api.openai.com/v1")

    with pytest.raises(ValueError, match="/v1"):
        validate_base_url(LLMProtocol.ANTHROPIC, "https://api.anthropic.com/v1")


@pytest.mark.parametrize("protocol", list(LLMProtocol))
def test_the_documented_root_for_each_protocol_passes(protocol):
    validate_base_url(protocol, BASE_URLS[protocol])


# ----------------------------------------------------------------------
# The 3 x 3 request matrix
# ----------------------------------------------------------------------


def payload_for(protocol: LLMProtocol, schema_mode: SchemaMode) -> dict:
    return build_payload(
        model="m",
        topic_text="分享一个开源小工具",
        candidates=CANDIDATES,
        max_output_tokens=1024,
        prompt_version="7",
        protocol=protocol,
        schema_mode=schema_mode,
    )


@pytest.mark.parametrize(("protocol", "schema_mode"), MATRIX)
def test_every_cell_names_the_model_and_bounds_the_output(protocol, schema_mode):
    payload = payload_for(protocol, schema_mode)

    assert payload["model"] == "m"
    # The field name differs; that a budget is sent does not.
    budget = payload.get("max_output_tokens", payload.get("max_tokens"))
    assert budget == 1024


@pytest.mark.parametrize(("protocol", "schema_mode"), MATRIX)
def test_every_cell_carries_the_prompt_and_the_candidate(protocol, schema_mode):
    payload = payload_for(protocol, schema_mode)
    body = json.dumps(payload, ensure_ascii=False)

    assert "PROMPT_VERSION: 7" in body
    assert CANDIDATE.canonical_url in body


@pytest.mark.parametrize(("protocol", "schema_mode"), MATRIX)
def test_no_cell_ever_sends_temperature_or_store(protocol, schema_mode):
    """Every field that is not required is one more thing a partially compatible
    endpoint can reject, and reasoning models reject `temperature` outright."""
    payload = payload_for(protocol, schema_mode)

    assert "temperature" not in payload
    assert "store" not in payload


@pytest.mark.parametrize("protocol", list(LLMProtocol))
def test_strict_mode_delivers_the_schema_structurally(protocol):
    """Each protocol wraps it differently; all three must actually send it."""
    payload = payload_for(protocol, SchemaMode.STRICT)
    schema = build_json_schema(CANDIDATES)

    if protocol is LLMProtocol.RESPONSES:
        assert payload["text"]["format"]["schema"] == schema
        assert payload["text"]["format"]["strict"] is True
    elif protocol is LLMProtocol.CHAT_COMPLETIONS:
        assert payload["response_format"]["json_schema"]["schema"] == schema
        assert payload["response_format"]["json_schema"]["strict"] is True
    else:
        assert payload["tools"][0]["input_schema"] == schema
        # Two fields, not one. Without forcing the tool the model may answer in
        # prose; without the tool-level `strict` flag the forced call binds field
        # names only. Either omission demotes the enum from grammar to request.
        assert payload["tool_choice"] == {"type": "tool", "name": SCHEMA_NAME}
        assert payload["tools"][0]["strict"] is True


@pytest.mark.parametrize("protocol", list(LLMProtocol))
@pytest.mark.parametrize("schema_mode", [SchemaMode.JSON_OBJECT, SchemaMode.NONE])
def test_a_degraded_mode_moves_the_schema_into_the_prompt(protocol, schema_mode):
    """The schema has nowhere else to go, and the prompt must say "json" — the
    `json_object` mode rejects a request that never mentions it."""
    payload = payload_for(protocol, schema_mode)
    body = json.dumps(payload, ensure_ascii=False)

    assert "json schema" in body
    assert "json" in body
    # The enum values still reach the model, as prose rather than as grammar.
    assert body.count(CANDIDATE.canonical_url) >= 2


@pytest.mark.parametrize("protocol", list(LLMProtocol))
def test_the_none_mode_sends_no_output_format_field_at_all(protocol):
    """Some local runtimes reject the field itself, not the schema inside it."""
    payload = payload_for(protocol, SchemaMode.NONE)

    assert "text" not in payload
    assert "response_format" not in payload
    assert "tools" not in payload
    assert "tool_choice" not in payload


def test_the_strict_responses_body_is_unchanged_by_the_protocol_split():
    """The regression that would be invisible: the default deployment's request
    body must be byte-identical to what it was before `llm/` existed."""
    implicit = build_payload(
        model="m",
        topic_text="分享一个开源小工具",
        candidates=CANDIDATES,
        max_output_tokens=1024,
        prompt_version="7",
    )

    assert json.dumps(implicit) == json.dumps(payload_for(LLMProtocol.RESPONSES, SchemaMode.STRICT))
    assert list(implicit) == ["model", "max_output_tokens", "input", "text"]


# ----------------------------------------------------------------------
# The 3 x 3 reply matrix
# ----------------------------------------------------------------------


@pytest.mark.parametrize(("protocol", "schema_mode"), MATRIX)
def test_every_cell_parses_a_well_formed_reply(protocol, schema_mode):
    reply = reply_for(protocol, schema_mode, json.dumps(DECISIONS, ensure_ascii=False))

    decisions = parse_response(reply, ALLOWED, protocol=protocol, schema_mode=schema_mode)

    assert [d.canonical_url for d in decisions] == [CANDIDATE.canonical_url]
    assert decisions[0].display_name == "tool"


@pytest.mark.parametrize(("protocol", "schema_mode"), MATRIX)
def test_the_allowlist_gate_holds_under_every_protocol_and_mode(protocol, schema_mode):
    """THE test. A weaker Schema Mode gives up the structural guard, so this is
    the one that must not depend on the mode — it is what makes degrading a cost
    in rejected decisions rather than a hole in the database."""
    reply = reply_for(protocol, schema_mode, json.dumps(FABRICATED))

    with pytest.raises(ClassifierError) as caught:
        parse_response(reply, ALLOWED, protocol=protocol, schema_mode=schema_mode)

    assert caught.value.category == CATEGORY_UNKNOWN_REPOSITORY
    assert caught.value.retryable is False


@pytest.mark.parametrize("protocol", list(LLMProtocol))
def test_a_markdown_fence_is_unwrapped_only_when_the_mode_is_degraded(protocol):
    """A fence under STRICT means the endpoint claimed strict schema support and
    did not deliver it. Smoothing that over would hide the lie the explicit mode
    exists to expose."""
    fenced = "```json\n" + json.dumps(DECISIONS, ensure_ascii=False) + "\n```"

    if protocol is not LLMProtocol.ANTHROPIC:
        # Anthropic's strict path reads a tool_use object, so a fenced string
        # cannot occur there; the other two carry text and must refuse it.
        strict_reply = reply_for(protocol, SchemaMode.STRICT, fenced)
        with pytest.raises(ClassifierError) as caught:
            parse_response(strict_reply, ALLOWED, protocol=protocol, schema_mode=SchemaMode.STRICT)
        assert caught.value.category == CATEGORY_MALFORMED_JSON

    degraded_reply = reply_for(protocol, SchemaMode.JSON_OBJECT, fenced)
    decisions = parse_response(
        degraded_reply, ALLOWED, protocol=protocol, schema_mode=SchemaMode.JSON_OBJECT
    )
    assert len(decisions) == 1


@pytest.mark.parametrize(("protocol", "schema_mode"), MATRIX)
def test_prose_around_the_json_is_never_salvaged(protocol, schema_mode):
    """Only a whole fenced block is unwrapped. Hunting for a JSON-looking
    substring inside prose is a guess, and a guess here becomes published data."""
    prose = "当然可以！这是结果：\n" + json.dumps(DECISIONS, ensure_ascii=False) + "\n希望有帮助。"

    if protocol is LLMProtocol.ANTHROPIC and schema_mode is SchemaMode.STRICT:
        pytest.skip("the strict anthropic path reads a tool_use object, never text")

    reply = reply_for(protocol, schema_mode, prose)

    with pytest.raises(ClassifierError) as caught:
        parse_response(reply, ALLOWED, protocol=protocol, schema_mode=schema_mode)

    assert caught.value.category == CATEGORY_MALFORMED_JSON
