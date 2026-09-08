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
    KNOWN_ENDPOINT_TAILS,
    LLMProtocol,
    SchemaMode,
    WireAdapter,
    endpoint_url,
    get_adapter,
    resolve_base_url,
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
def test_the_documented_root_for_each_protocol_is_returned_unchanged(protocol):
    root = BASE_URLS[protocol]
    assert resolve_base_url(protocol, root) == root


@pytest.mark.parametrize("protocol", list(LLMProtocol))
def test_a_paste_of_the_protocols_own_endpoint_is_stripped_back_to_the_root(protocol):
    """Class A: the ending is one only a paste of *this* endpoint can produce, so
    the intent is unambiguous and the value is normalised rather than refused."""
    root = BASE_URLS[protocol]
    pasted = root + get_adapter(protocol).ENDPOINT_SUFFIX

    assert resolve_base_url(protocol, pasted) == root


def test_anthropic_strips_the_v1_root_it_used_to_reject():
    """The one behaviour that flipped. `/v1` is a path prefix of `/v1/messages`,
    which is why no per-adapter suffix list is needed to say so."""
    assert (
        resolve_base_url(LLMProtocol.ANTHROPIC, "https://api.anthropic.com/v1")
        == "https://api.anthropic.com"
    )


def test_chat_completions_strips_a_half_pasted_chat_segment():
    """`/chat` is a path prefix of `/chat/completions`, so it falls out of the same
    rule. Stripping it and re-appending the suffix reproduces the correct endpoint,
    which is why it gets no exception carved out for it."""
    assert (
        resolve_base_url(LLMProtocol.CHAT_COMPLETIONS, "https://gw.example.com/v1/chat")
        == "https://gw.example.com/v1"
    )


def test_a_v1_root_is_untouched_under_the_openai_style_protocols():
    """The asymmetry is the whole reason the rule is per-protocol: `/v1` is a
    correct root here, and stripping it would break a working configuration."""
    for protocol in (LLMProtocol.RESPONSES, LLMProtocol.CHAT_COMPLETIONS):
        assert resolve_base_url(protocol, "https://api.openai.com/v1") == (
            "https://api.openai.com/v1"
        )


@pytest.mark.parametrize(
    ("protocol", "tail"),
    [
        (protocol, tail)
        for tail, owner in KNOWN_ENDPOINT_TAILS.items()
        for protocol in LLMProtocol
        if owner is not protocol
    ],
)
def test_another_protocols_endpoint_path_is_refused_and_names_that_protocol(protocol, tail):
    """Class B: the paste is not the mistake, LLM_PROTOCOL is. Stripping it would
    yield a root that validates and then 404s from a cron run."""
    owner = KNOWN_ENDPOINT_TAILS[tail]

    with pytest.raises(ValueError, match="LLM_BASE_URL") as caught:
        resolve_base_url(protocol, f"https://api.example.com/v1{tail}")

    message = str(caught.value)
    assert owner.value in message
    assert "LLM_PROTOCOL" in message


def test_anthropic_refuses_a_v1_less_messages_path_it_cannot_strip():
    """`/messages` is anthropic's own ending yet not a prefix of `/v1/messages`, so
    no strip could produce a correct root. The remedy is deletion, and the message
    must not suggest switching protocol to the one already selected."""
    with pytest.raises(ValueError, match="/messages") as caught:
        resolve_base_url(LLMProtocol.ANTHROPIC, "https://gw.example.com/messages")

    assert "/v1/messages" in str(caught.value)


def test_one_strip_only_a_doubled_endpoint_path_still_fails():
    """Stripping twice would be guessing. One strip, then the map is re-checked."""
    with pytest.raises(ValueError, match="/responses"):
        resolve_base_url(LLMProtocol.RESPONSES, "https://gw.example.com/responses/responses")


def test_a_strip_may_not_eat_into_the_authority():
    """`"https://v1".endswith("/v1")` is True, so the naive strip returns `https:/`.
    This is the boundary the strip itself introduces."""
    with pytest.raises(ValueError, match="no host"):
        resolve_base_url(LLMProtocol.ANTHROPIC, "https://v1")


@pytest.mark.parametrize("trailing", ["", "/", "///"])
@pytest.mark.parametrize("protocol", list(LLMProtocol))
def test_trailing_slashes_change_neither_the_strip_nor_the_root(protocol, trailing):
    root = BASE_URLS[protocol]
    pasted = root + get_adapter(protocol).ENDPOINT_SUFFIX + trailing

    assert resolve_base_url(protocol, pasted) == root
    assert resolve_base_url(protocol, root + trailing) == root


@pytest.mark.parametrize("protocol", list(LLMProtocol))
def test_no_base_url_message_ever_echoes_the_host(protocol):
    """A gateway base URL can carry a key in a query string, so the rule is
    absolute for every rejection path in this function."""
    host = "secret-host.example.com"

    for bad in (f"https://{host}/v1?token=k", f"https://{host}/v1#f"):
        with pytest.raises(ValueError) as caught:
            resolve_base_url(protocol, bad)
        assert host not in str(caught.value)


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
