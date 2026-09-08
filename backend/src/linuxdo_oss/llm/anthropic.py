"""Anthropic Messages API adapter.

    POST <root>/v1/messages
    headers: x-api-key: <key>, anthropic-version: 2023-06-01
    {"model": ..., "max_tokens": ...,
     "system": <the system prompt, a TOP-LEVEL field>,
     "messages": [{"role": "user", ...}],
     "tools": [{"name": ..., "input_schema": ...}],
     "tool_choice": {"type": "tool", "name": ...}}

Three things differ from both OpenAI-style adapters, and each one is a silent
failure if inherited from them:

1. **Auth is `x-api-key`, not `Authorization: Bearer`**, plus a required
   `anthropic-version`. A Bearer header here is simply not authenticated.
2. **The system prompt is a top-level `system` string**, not a message with
   `role: "system"`. A system-role message is rejected outright.
3. **Structured output is a forced tool call**, not a response format:
   `tool_choice` names one tool whose `input_schema` is the target shape, and the
   reply carries the object in `tool_use.input` already parsed.

   This is WEAKER than the two OpenAI adapters, and the difference is
   load-bearing. Anthropic gates grammar-constrained sampling on a separate
   `strict: true` flag on the tool definition; without it a forced tool call
   binds the field NAMES but is documented as best effort about types and
   required fields. So under this protocol the `canonical_url` enum is a strong
   instruction, not a grammar the sampler cannot leave, and `SchemaMode.STRICT`
   here does not mean quite what it means under `responses`.

   `strict: true` is deliberately not sent yet: `[UNKNOWN]` whether the
   Anthropic-compatible relays this project actually targets accept the flag or
   reject the request over it, and this codebase does not guess about wire
   compatibility. Settle that before adding it.

   Either way the database is protected. `_reject_unknown_and_duplicate` in
   `classifier.py` re-checks every URL against `allowed_urls` under every
   protocol and every Schema Mode, and that — not the schema — is what makes a
   fabricated repository unpublishable.

The base URL convention is also the opposite of OpenAI's: the root is
`https://api.anthropic.com` with NO `/v1`, because the version sits in the path
this module appends. `FORBIDDEN_BASE_SUFFIXES` catches the paste-o that would
otherwise produce `/v1/v1/messages`.
"""

from __future__ import annotations

from typing import Any

from linuxdo_oss.llm.errors import (
    CATEGORY_INCOMPLETE,
    CATEGORY_INCOMPLETE_MAX_TOKENS,
    CATEGORY_NO_OUTPUT_TEXT,
    CATEGORY_PROVIDER_FAILED,
    CATEGORY_REFUSAL,
    ClassifierError,
    clamp_token,
)
from linuxdo_oss.llm.json_text import loads_structured_json
from linuxdo_oss.llm.protocol import SchemaMode

__all__ = [
    "ANTHROPIC_VERSION",
    "ENDPOINT_SUFFIX",
    "FORBIDDEN_BASE_SUFFIXES",
    "auth_headers",
    "build_payload",
    "extract_structured_output",
]

ENDPOINT_SUFFIX = "/v1/messages"

# The version lives in ENDPOINT_SUFFIX, so a root that already ends in `/v1` is
# the one paste-o that produces `/v1/v1/messages` — a 404 that reads like a
# network problem. Caught at startup by `protocol.validate_base_url`.
FORBIDDEN_BASE_SUFFIXES: tuple[str, ...] = ("/v1",)

# Pinned, not tracked. The Messages API requires this header and treats it as the
# contract for the response shape this module parses; following the newest version
# without re-reading the reply shape is how a parser silently stops matching.
ANTHROPIC_VERSION = "2023-06-01"


def auth_headers(api_key: str) -> dict[str, str]:
    return {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}


def build_payload(
    *,
    model: str,
    system_prompt: str,
    user_content: str,
    json_schema: dict[str, Any],
    schema_name: str,
    schema_mode: SchemaMode,
    max_output_tokens: int,
) -> dict[str, Any]:
    """The complete request body. Pure — no network, no config.

    `max_tokens` is REQUIRED by this API, unlike the two OpenAI-style protocols
    where it is optional. Omitting it is a 400.
    """
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_output_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
    }

    if schema_mode is SchemaMode.STRICT:
        payload["tools"] = [
            {
                "name": schema_name,
                "description": (
                    "Record one decision per candidate repository. Call this tool exactly once."
                ),
                "input_schema": json_schema,
            }
        ]
        # Forcing the tool is what makes the schema binding. With `auto` the model
        # may answer in prose and the enum stops being a constraint.
        payload["tool_choice"] = {"type": "tool", "name": schema_name}

    # SchemaMode.JSON_OBJECT and SchemaMode.NONE are the same request here: this
    # API has no response-format field, so both degrade to a plain message whose
    # schema travels in the prompt. They stay distinct in configuration because
    # they are not the same request under the other two protocols.

    return payload


def extract_structured_output(raw: dict[str, Any], schema_mode: SchemaMode) -> Any:
    """The structured output as a Python object, or raise.

    Unlike the OpenAI-style adapters, `schema_mode` genuinely changes where the
    output sits: a forced tool call puts it in `tool_use.input` as an already
    parsed object, while a degraded request puts it in `text` as a JSON string.
    """
    _reject_error_envelope(raw)

    blocks = raw.get("content")
    if not isinstance(blocks, list):
        raise ClassifierError(
            "response has no `content` array",
            category=CATEGORY_NO_OUTPUT_TEXT,
            retryable=False,
        )

    # Refusal and truncation are checked before the content is read, and refusal
    # first: a truncated refusal is still a refusal, and reporting it as
    # truncation would send an operator after the token budget.
    _reject_refusal_or_truncation(raw.get("stop_reason"))

    if schema_mode is SchemaMode.STRICT:
        return _tool_use_input(blocks)

    return loads_structured_json(_text_block(blocks), schema_mode)


def _reject_error_envelope(raw: dict[str, Any]) -> None:
    """`{"type": "error", "error": {...}}` — returned with a 200 by some proxies."""
    if raw.get("type") != "error":
        return

    error = raw.get("error")
    code = error.get("type") if isinstance(error, dict) else None
    raise ClassifierError(
        f"provider returned an error envelope (type={clamp_token(code)})",
        category=CATEGORY_PROVIDER_FAILED,
        retryable=True,
    )


def _reject_refusal_or_truncation(stop_reason: object) -> None:
    if stop_reason == "refusal":
        raise ClassifierError(
            "model refused the request",
            category=CATEGORY_REFUSAL,
            retryable=False,
        )

    if stop_reason == "max_tokens":
        # Not retryable: the same request with the same budget truncates again.
        raise ClassifierError(
            "response truncated at max_tokens",
            category=CATEGORY_INCOMPLETE_MAX_TOKENS,
            retryable=False,
        )

    if stop_reason == "model_context_window_exceeded":
        # A documented stop_reason, and a different failure from `max_tokens`: the
        # INPUT filled the window, so raising the output budget cannot help — the
        # fix is a shorter topic text or fewer candidates. Named rather than left
        # to fall through, because falling through reached `_tool_use_input` and
        # reported "the endpoint ignored tool_choice" for what is really a
        # truncated reply. That is the same misdiagnosis CATEGORY_ENDPOINT_CONFIG
        # exists to remove.
        raise ClassifierError(
            f"response truncated (stop_reason={clamp_token(stop_reason)})",
            category=CATEGORY_INCOMPLETE,
            retryable=False,
        )


def _tool_use_input(blocks: list[Any]) -> dict[str, Any]:
    """The `input` of the first `tool_use` block.

    Never by index: the model may emit a `text` or `thinking` block ahead of the
    tool call, exactly as a reasoning model does under the Responses API.

    The tool NAME is not matched. `tool_choice` named exactly one tool and the
    request carries no others, so a `tool_use` block here can only be that tool;
    requiring the name to echo would reject a compatible endpoint that normalizes
    it.
    """
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue

        payload = block.get("input")
        if isinstance(payload, dict):
            return payload

        raise ClassifierError(
            "tool_use block carried a non-object `input`",
            category=CATEGORY_NO_OUTPUT_TEXT,
            retryable=False,
        )

    raise ClassifierError(
        "no `tool_use` block in the reply; the endpoint ignored tool_choice",
        category=CATEGORY_NO_OUTPUT_TEXT,
        retryable=False,
    )


def _text_block(blocks: list[Any]) -> str:
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue

        text = block.get("text")
        if isinstance(text, str) and text.strip():
            return text

    raise ClassifierError(
        "no `text` block in the reply",
        category=CATEGORY_NO_OUTPUT_TEXT,
        retryable=False,
    )
