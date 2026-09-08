"""Anthropic Messages API adapter.

    POST <root>/v1/messages
    headers: x-api-key: <key>, anthropic-version: 2023-06-01
    {"model": ..., "max_tokens": ...,
     "system": <the system prompt, a TOP-LEVEL field>,
     "messages": [{"role": "user", ...}],
     "tools": [{"name": ..., "input_schema": ..., "strict": true}],
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

   `SchemaMode.STRICT` therefore needs TWO fields, and the second one is the
   load-bearing one. `tool_choice` only makes the model call the tool instead of
   answering in prose; the tool definition's own `strict: true` is what gates
   grammar-constrained sampling. A forced tool call WITHOUT that flag binds the
   field NAMES but is documented as best effort about types and required fields,
   which would leave the `canonical_url` enum a strong instruction rather than a
   grammar the sampler cannot leave. Both are sent, so STRICT means here exactly
   what it means under `responses` and `chat_completions`.

   The flag is GA — no beta header — and requires the schema to carry
   `additionalProperties: false` on every object and every property in
   `required`. `classifier.build_json_schema` already satisfies both, because
   OpenAI strict mode demands the same two things, so nothing about the schema
   is protocol-specific.

   An endpoint that does not recognise the flag answers 4xx, which
   `CATEGORY_ENDPOINT_CONFIG` reports as a configuration problem naming
   `LLM_PROTOCOL` / `LLM_BASE_URL` / `LLM_API_KEY`. That is deliberate: the
   escape hatch is `LLM_SCHEMA_MODE=json_object` or `none`, declared in
   deployment configuration — never a retry with the flag dropped, which is the
   silent fallback ADR 0005 forbids.

   Either way the database is protected. `_reject_unknown_and_duplicate` in
   `classifier.py` re-checks every URL against `allowed_urls` under every
   protocol and every Schema Mode, and that — not the schema — is what makes a
   fabricated repository unpublishable.

The base URL convention is also the opposite of OpenAI's: the root is
`https://api.anthropic.com` with NO `/v1`, because the version sits in the path
this module appends. A configured root that still carries `/v1` is stripped back
by `protocol.resolve_base_url` rather than rejected — `/v1` is a path prefix of
`ENDPOINT_SUFFIX`, so the rule needs no declaration here.
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
    "auth_headers",
    "build_payload",
    "extract_structured_output",
]

ENDPOINT_SUFFIX = "/v1/messages"

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
                # A TOP-LEVEL field on the tool definition, never a key inside
                # `tool_choice`, and this is the one that switches on
                # grammar-constrained sampling. Without it a forced tool call binds
                # the field NAMES only and is best effort about types and required
                # fields, which would leave the `canonical_url` enum an instruction
                # instead of a constraint. GA, no beta header.
                "strict": True,
            }
        ]
        # Forcing the tool is what makes the schema reachable at all. With `auto`
        # the model may answer in prose and the enum stops being a constraint even
        # as an instruction.
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
