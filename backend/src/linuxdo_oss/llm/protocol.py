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
    "BASE_URL_FORBIDDEN_SUFFIXES",
    "LLMProtocol",
    "SchemaMode",
    "WireAdapter",
    "endpoint_url",
    "get_adapter",
    "parse_protocol",
    "parse_schema_mode",
    "validate_base_url",
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

    `STRICT` asks the endpoint to enforce the schema, which under `responses` and
    `chat_completions` makes the `canonical_url` enum a constraint the sampler
    cannot violate. Under `anthropic` it is weaker: a forced tool call without
    Anthropic's separate `strict: true` tool flag is documented as best effort —
    see the note in `llm/anthropic.py`. The other two modes are for endpoints
    that reject a schema outright; they move it into the prompt.

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

    #: Appended to the configured API root to reach the endpoint.
    ENDPOINT_SUFFIX: str
    #: Base-URL endings that are wrong *for this protocol specifically*.
    FORBIDDEN_BASE_SUFFIXES: tuple[str, ...]

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


# Endings that mean the operator pasted a full endpoint path into what must be an
# API *root*. Wrong under every protocol, because the suffix is the adapter's job:
# left alone they produce `/responses/responses` and a 404 that reads like a
# network problem.
BASE_URL_FORBIDDEN_SUFFIXES = ("/responses", "/chat/completions", "/messages")


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


def validate_base_url(protocol: LLMProtocol, base_url: str) -> None:
    """Raise if the configured root cannot produce a correct endpoint URL.

    Called from `config.py` at startup, so a paste-o fails once and loudly with a
    message naming the rule — rather than as a 404 five minutes later, from a cron
    run, in a category that used to be indistinguishable from a timeout.

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

    normalized = base_url.rstrip("/")

    for suffix in (*BASE_URL_FORBIDDEN_SUFFIXES, *adapter.FORBIDDEN_BASE_SUFFIXES):
        if normalized.endswith(suffix):
            raise ValueError(
                f"LLM_BASE_URL must be an API root, not a full endpoint path: "
                f"remove the trailing {suffix!r} "
                f"(protocol {protocol.value} appends {adapter.ENDPOINT_SUFFIX!r} itself)"
            )


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
        # `validate_base_url`, which must not echo its input at all.
        raise ValueError(f"{name} must be one of: {allowed} (got {text[:40]!r})") from None
