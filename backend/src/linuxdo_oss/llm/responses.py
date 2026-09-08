"""OpenAI Responses API adapter.

The project's original and default protocol. Verified against the OpenAI
Structured Outputs guide (2026-08-31):

    POST <root>/responses
    {"model": ..., "max_output_tokens": ...,
     "input": [{"role": "system", ...}, {"role": "user", ...}],
     "text": {"format": {"type": "json_schema", "name": ..., "schema": ...,
                         "strict": true}}}

`text.format`, never Chat Completions' `response_format` — that spelling belongs
to the sibling module, and sending it here is a request a compatible endpoint is
entitled to reject.

The reply is walked as typed output items — a message item's `content` entry of
type `output_text` — never by index, because reasoning models put a `reasoning`
item in front of the message and the top-level `output_text` convenience field is
an SDK affordance, not part of what a compatible endpoint must serve.
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
    "ENDPOINT_SUFFIX",
    "auth_headers",
    "build_payload",
    "extract_structured_output",
]

ENDPOINT_SUFFIX = "/responses"


def auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


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

    Kept to the four fields the contract needs. `temperature` is omitted because
    reasoning models reject it, and `store` is omitted because every extra field
    is one more thing a partially compatible endpoint can choke on.

    Key order is deliberate and load-bearing for one test: under
    `SchemaMode.STRICT` this body must stay byte-identical to what the module
    produced before the protocol split, so the existing wire-contract test passes
    unchanged.
    """
    payload: dict[str, Any] = {
        "model": model,
        "max_output_tokens": max_output_tokens,
        "input": [
            # "system" rather than "developer": both are accepted by the Responses
            # API, and `system` is the spelling a third-party compatible endpoint
            # is most likely to implement.
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }

    text_format = _text_format(json_schema, schema_name, schema_mode)
    if text_format is not None:
        payload["text"] = {"format": text_format}

    return payload


def _text_format(
    json_schema: dict[str, Any], schema_name: str, schema_mode: SchemaMode
) -> dict[str, Any] | None:
    if schema_mode is SchemaMode.STRICT:
        return {
            "type": "json_schema",
            "name": schema_name,
            "schema": json_schema,
            "strict": True,
        }

    if schema_mode is SchemaMode.JSON_OBJECT:
        return {"type": "json_object"}

    # SchemaMode.NONE: send no `text` key at all. Some endpoints reject the field
    # itself rather than the schema inside it.
    return None


def extract_structured_output(raw: dict[str, Any], schema_mode: SchemaMode) -> Any:
    """The structured output as a Python object, or raise.

    `schema_mode` does not change *where* the output sits — a Responses reply
    always carries it as `output_text` — only how strictly it must already be
    JSON. See `json_text.loads_structured_json`.
    """
    _reject_failed_or_incomplete(raw)

    return loads_structured_json(_extract_output_text(raw), schema_mode)


def _reject_failed_or_incomplete(raw: dict[str, Any]) -> None:
    """Handle the two terminal statuses before looking for output.

    A missing `status` is tolerated: it is informational, the parsed output is the
    authority, and refusing a reply that carries valid structured output would
    reject a compatible endpoint over a field this module does not need. An
    in-progress reply falls through and fails as `no_output_text`, which is the
    truth about it.
    """
    status = raw.get("status")

    if status == "failed":
        error = raw.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        raise ClassifierError(
            f"provider reported status=failed (code={clamp_token(code)})",
            category=CATEGORY_PROVIDER_FAILED,
            retryable=True,
        )

    if status == "incomplete":
        details = raw.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None

        if reason == "max_output_tokens":
            # Not retryable: the same request with the same budget truncates again.
            # The fix is a bigger LLM_MAX_OUTPUT_TOKENS or fewer candidates, and
            # the category is what says so.
            raise ClassifierError(
                "response truncated at max_output_tokens",
                category=CATEGORY_INCOMPLETE_MAX_TOKENS,
                retryable=False,
            )

        raise ClassifierError(
            f"response incomplete (reason={clamp_token(reason)})",
            category=CATEGORY_INCOMPLETE,
            retryable=False,
        )


def _extract_output_text(raw: dict[str, Any]) -> str:
    """Walk typed output items for the structured text.

    Never by index: a reasoning model emits a `reasoning` item ahead of the
    message, and the ordering of `output` is the provider's business.
    """
    items = raw.get("output")
    if not isinstance(items, list):
        raise ClassifierError(
            "response has no `output` array",
            category=CATEGORY_NO_OUTPUT_TEXT,
            retryable=False,
        )

    messages = [item for item in items if isinstance(item, dict) and item.get("type") == "message"]

    # Refusals are checked across every message BEFORE any text is accepted. A
    # refusal is a hard stop and must not be overridden by adjacent output_text,
    # whatever order the items arrive in.
    for part in _content_parts(messages):
        if part.get("type") == "refusal":
            raise ClassifierError(
                "model refused the request",
                category=CATEGORY_REFUSAL,
                retryable=False,
            )

    for part in _content_parts(messages):
        if part.get("type") == "output_text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                return text

    raise ClassifierError(
        "no message item carried an `output_text` content entry",
        category=CATEGORY_NO_OUTPUT_TEXT,
        retryable=False,
    )


def _content_parts(messages: list[dict[str, Any]]):
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict):
                yield part
