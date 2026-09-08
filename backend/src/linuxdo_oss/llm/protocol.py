"""The two configuration enums and the dispatch that picks one wire adapter.

Seven things differ per protocol — URL suffix, auth header, output-token field
name, where the system prompt goes, how a JSON Schema is wrapped, where the
structured output sits in the reply, and how a failure or refusal announces
itself. Multiply by three Schema Modes and an `if` chain becomes a 3x3 matrix
smeared across every function. So each protocol is one module implementing one
interface, dispatch happens exactly once, and callers branch zero times.

Dispatch imports the selected adapter *inside* `get_adapter`. Only one protocol is
ever configured in a deployment, and a Worker pays for every module it imports
against a 1 s startup limit — importing the two that will never run is pure cost.

Everything in this package is stdlib-only and must stay that way: no `workers`,
no `js`, no `pydantic`. That is what lets the whole matrix be tested under plain
CPython pytest.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "KNOWN_ENDPOINT_TAILS",
    "LLMProtocol",
    "SchemaMode",
    "WireAdapter",
    "endpoint_url",
    "get_adapter",
    "parse_protocol",
    "parse_schema_mode",
    "resolve_base_url",
]


class LLMProtocol(StrEnum):
    """The wire protocol the Classifier Endpoint is expected to speak.

    `StrEnum` so a member compares equal to its configured spelling and renders
    in a log or an f-string as `responses`, not `LLMProtocol.RESPONSES`. That
    matters: these values are read from and reported back to deployment config,
    and a plain `Enum` mixin stringifies to the member name under Python 3.12+.
    """

    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"
    ANTHROPIC = "anthropic"


class SchemaMode(StrEnum):
    """How much of the JSON Schema actually reaches the model.

    `STRICT` asks the endpoint to enforce the schema, which makes the
    `canonical_url` enum a constraint the sampler cannot violate under all three
    protocols. Each one carries its own `strict` flag beside the schema;
    `anthropic`'s sits on the forced tool definition, and without it a forced tool
    call would bind only the field names — see the note in `llm/anthropic.py`. The
    other two modes are for endpoints that reject a schema outright; they move it
    into the prompt.

    In every case the anti-hallucination guarantee rests on the `allowed_urls`
    re-check in `classifier.py`, which is where it rested anyway. That is why the
    difference costs rejected decisions rather than data integrity. See
    `docs/adr/0005-llm-multi-protocol.md`.

    Never selected at runtime. A weaker mode is a deployment decision, and a
    request that fails is never retried under one.
    """

    STRICT = "strict"
    JSON_OBJECT = "json_object"
    NONE = "none"


@runtime_checkable
class WireAdapter(Protocol):
    """What every protocol module in this package provides.

    Structural, not inherited: `get_adapter` returns a module, and a module cannot
    subclass anything. The point is that the shape is stated once here instead of
    living only in three files that are expected to agree.
    """

    #: Appended to the configured API root to reach the endpoint. Also the sole
    #: source of the endings `resolve_base_url` strips: every non-empty path
    #: prefix of this value is an ending only a paste of *this* protocol's own
    #: endpoint can produce, so no protocol declares a suffix list of its own.
    ENDPOINT_SUFFIX: str

    def auth_headers(self, api_key: str) -> dict[str, str]: ...

    def build_payload(
        self,
        *,
        model: str,
        system_prompt: str,
        user_content: str,
        json_schema: dict[str, Any],
        schema_name: str,
        schema_mode: SchemaMode,
        max_output_tokens: int,
    ) -> dict[str, Any]: ...

    def extract_structured_output(self, raw: dict[str, Any], schema_mode: SchemaMode) -> Any: ...


# Every endpoint ending this project recognises, mapped to the protocol that owns
# it. Hand-written rather than gathered from the three adapters: reading
# `ENDPOINT_SUFFIX` off all of them would import all three, and `get_adapter`
# imports lazily precisely to avoid paying startup cost for the two that never run.
#
# `/messages` and not `/v1/messages`, because the shorter ending also catches the
# longer one and additionally catches a gateway that mounts Messages without `/v1`.
KNOWN_ENDPOINT_TAILS: dict[str, LLMProtocol] = {
    "/responses": LLMProtocol.RESPONSES,
    "/chat/completions": LLMProtocol.CHAT_COMPLETIONS,
    "/messages": LLMProtocol.ANTHROPIC,
}


def get_adapter(protocol: LLMProtocol) -> WireAdapter:
    """The module implementing `protocol`. Imported lazily, one per deployment."""
    if protocol is LLMProtocol.RESPONSES:
        from linuxdo_oss.llm import responses

        return responses

    if protocol is LLMProtocol.CHAT_COMPLETIONS:
        from linuxdo_oss.llm import chat_completions

        return chat_completions

    from linuxdo_oss.llm import anthropic

    return anthropic


def endpoint_url(protocol: LLMProtocol, base_url: str) -> str:
    """`<root><suffix>`, tolerating a trailing slash on the root."""
    return f"{base_url.rstrip('/')}{get_adapter(protocol).ENDPOINT_SUFFIX}"


def _own_endpoint_tails(endpoint_suffix: str) -> tuple[str, ...]:
    """`endpoint_suffix`'s non-empty path prefixes, longest first.

    `/responses` -> `/responses`; `/chat/completions` -> `/chat/completions`,
    `/chat`; `/v1/messages` -> `/v1/messages`, `/v1`.

    These are exactly the endings that only a paste of *this* protocol's own
    endpoint can produce, which is why they are the ones `resolve_base_url` may
    strip. Deriving them is also what retires the per-adapter suffix lists:
    `anthropic` needed one solely because `/v1` is a prefix of `/v1/messages`, and
    that now follows from the rule instead of being restated beside it.

    Longest first, so `/chat/completions` is consumed whole rather than matching
    `/chat` and leaving a stray `/completions` behind.
    """
    segments = endpoint_suffix.strip("/").split("/")
    return tuple("/" + "/".join(segments[:count]) for count in range(len(segments), 0, -1))


def resolve_base_url(protocol: LLMProtocol, base_url: str) -> str:
    """The API root `base_url` denotes, or `ValueError` if it denotes none.

    A trailing copy of *this* protocol's own endpoint path is removed, because
    pasting the endpoint URL out of a provider's documentation is the ordinary way
    this value gets configured and the intent is then unambiguous. The result is
    always a prefix of the stripped input, so a caller that wants to report what
    went away can subtract it — that is how `config.py` names the suffix in its
    WARNING without this function needing a second return value.

    An ending owned by a *different* protocol is refused instead. There the paste
    is not the mistake, `LLM_PROTOCOL` is, and stripping it would produce a root
    that validates and then 404s from a cron run five minutes later — the
    diagnosability defect ADR 0005 exists to remove. See
    `docs/adr/0006-llm-base-url-normalization.md`.

    No message here echoes the URL, only the rule it broke: a gateway base URL can
    carry a key in a query string, and the logging spec is absolute about
    configuration values.
    """
    adapter = get_adapter(protocol)

    # A query string or fragment makes the whole scheme unworkable, not merely
    # untidy: `endpoint_url` appends a PATH, so `https://h/v1?k=1` would become
    # `https://h/v1?k=1/responses` — a request to a path inside the query string.
    # Refused rather than accommodated, because supporting it would mean parsing
    # and reassembling the URL, and no endpoint this project targets needs it.
    for character in ("?", "#"):
        if character in base_url:
            raise ValueError(
                f"LLM_BASE_URL must not contain {character!r}: the protocol's path "
                f"suffix ({adapter.ENDPOINT_SUFFIX!r}) is appended to it, so a query "
                f"string or fragment cannot be preserved"
            )

    root = base_url.rstrip("/")

    for tail in _own_endpoint_tails(adapter.ENDPOINT_SUFFIX):
        if not root.endswith(tail):
            continue

        root = root[: -len(tail)]

        # `"https://v1".endswith("/v1")` is True, so a strip can eat into the
        # authority and leave `https:/`. Checked here and only here, because this
        # is the one failure the strip itself introduces; the `https://` rule
        # stays in `config.py`, which owns it and has its own message for it.
        if not root.partition("://")[2]:
            raise ValueError(
                f"LLM_BASE_URL is not a usable API root: removing the trailing "
                f"{tail!r} leaves no host"
            )

        break

    # Matched against the map *after* the strip, deliberately: one strip is all
    # this function does. An ending still standing here is the wrong protocol or a
    # genuinely malformed value, and both are worth failing loudly for rather than
    # guessing at a second time.
    for tail, owner in KNOWN_ENDPOINT_TAILS.items():
        if not root.endswith(tail):
            continue

        if owner is protocol:
            # `/messages` under `anthropic` lands here: the ending is this
            # protocol's own, yet not a prefix of `/v1/messages`, so no strip could
            # have produced a correct root. There is nothing to suggest but
            # deleting it.
            remedy = f"remove it — {protocol.value} appends {adapter.ENDPOINT_SUFFIX!r} itself"
        else:
            remedy = (
                f"it is the endpoint path of protocol {owner.value} while "
                f"LLM_PROTOCOL is {protocol.value}, so either remove it or set "
                f"LLM_PROTOCOL={owner.value}"
            )

        raise ValueError(f"LLM_BASE_URL must be an API root but ends in {tail!r}: {remedy}")

    return root


def parse_protocol(value: object) -> LLMProtocol:
    """Parse `LLM_PROTOCOL`. An unrecognised value raises rather than defaulting.

    Silently falling back to `responses` on a typo is the failure this whole task
    exists to remove: the operator would see 404s from an endpoint that speaks a
    protocol they thought they had selected.
    """
    return _parse_enum(LLMProtocol, value, "LLM_PROTOCOL")


def parse_schema_mode(value: object) -> SchemaMode:
    """Parse `LLM_SCHEMA_MODE`. An unrecognised value raises rather than defaulting."""
    return _parse_enum(SchemaMode, value, "LLM_SCHEMA_MODE")


def _parse_enum(enum_type: Any, value: object, name: str) -> Any:
    text = str(value).strip().lower()

    try:
        return enum_type(text)
    except ValueError:
        allowed = ", ".join(member.value for member in enum_type)
        # The value IS echoed here, and only here: it is an enum spelling the
        # operator typed, truncated, and never a secret — whereas a message that
        # only said "invalid" would send them hunting. Contrast
        # `resolve_base_url`, which must not echo its input at all.
        raise ValueError(f"{name} must be one of: {allowed} (got {text[:40]!r})") from None
