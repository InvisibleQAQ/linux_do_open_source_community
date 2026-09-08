"""OpenAI Chat Completions adapter — the broad-compatibility protocol.

    POST <root>/chat/completions
    {"model": ..., "max_tokens": ...,
     "messages": [{"role": "system", ...}, {"role": "user", ...}],
     "response_format": {"type": "json_schema",
                         "json_schema": {"name": ..., "schema": ...,
                                         "strict": true}}}

This exists because `/v1/responses` is a minority endpoint outside OpenAI itself:
vLLM, Ollama, LM Studio and most relay gateways implement only
`/v1/chat/completions`. Gemini is reachable here too, through its OpenAI
compatibility layer (`/v1beta/openai/`), which is why this project ships no native
`generateContent` adapter.

Two spellings worth stating, because both look like mistakes and are not:

* `max_tokens`, not `max_completion_tokens`. OpenAI now prefers the latter, but
  the entire reason this protocol exists is third-party coverage, and `max_tokens`
  is the field those endpoints actually implement.
* `response_format.json_schema.strict`, one level deeper than the Responses API's
  `text.format.strict`. Same capability, different nesting; getting it wrong
  produces a schema that is accepted and silently ignored.
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
    "FORBIDDEN_BASE_SUFFIXES",
    "auth_headers",
    "build_payload",
    "extract_structured_output",
]

ENDPOINT_SUFFIX = "/chat/completions"

# Nothing beyond the shared set: an OpenAI-style root conventionally ends in
# `/v1`, so that ending is correct here rather than wrong.
FORBIDDEN_BASE_SUFFIXES: tuple[str, ...] = ()


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

    `temperature` is omitted for the same reason as in the Responses adapter:
    reasoning models reject it, and every field that is not required is one more
    thing a partially compatible endpoint can refuse.
    """
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_output_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }

    response_format = _response_format(json_schema, schema_name, schema_mode)
    if response_format is not None:
        payload["response_format"] = response_format

    return payload


def _response_format(
    json_schema: dict[str, Any], schema_name: str, schema_mode: SchemaMode
) -> dict[str, Any] | None:
    if schema_mode is SchemaMode.STRICT:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": json_schema,
                "strict": True,
            },
        }

    if schema_mode is SchemaMode.JSON_OBJECT:
        # DeepSeek and ZhiPu accept exactly this and 400 on `json_schema`. The
        # schema still reaches the model, as prose, through the prompt.
        return {"type": "json_object"}

    # SchemaMode.NONE: some local runtimes reject the `response_format` key
    # itself, so it is omitted rather than emptied.
    return None


def extract_structured_output(raw: dict[str, Any], schema_mode: SchemaMode) -> Any:
    """The structured output as a Python object, or raise.

    `schema_mode` does not change *where* the output sits — always
    `choices[0].message.content` — only how strictly it must already be JSON.
    See `json_text.loads_structured_json`.
    """
    _reject_provider_error(raw)

    message, finish_reason = _first_choice(raw)

    _reject_refusal(message)
    _reject_truncated(finish_reason)

    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ClassifierError(
            "choices[0].message carried no text content",
            category=CATEGORY_NO_OUTPUT_TEXT,
            retryable=False,
        )

    return loads_structured_json(content, schema_mode)


def _reject_provider_error(raw: dict[str, Any]) -> None:
    """Relay gateways answer 200 with an `error` body instead of an HTTP status.

    Retryable: the shape says the provider owns the failure, and a gateway that
    reports upstream capacity this way is exactly the case a later cron run fixes.
    """
    error = raw.get("error")
    if not isinstance(error, dict):
        return

    raise ClassifierError(
        f"provider returned an error body (code={clamp_token(error.get('code'))})",
        category=CATEGORY_PROVIDER_FAILED,
        retryable=True,
    )


def _first_choice(raw: dict[str, Any]) -> tuple[dict[str, Any], object]:
    choices = raw.get("choices")

    if not isinstance(choices, list) or not choices:
        raise ClassifierError(
            "response has no `choices` array",
            category=CATEGORY_NO_OUTPUT_TEXT,
            retryable=False,
        )

    # Index 0 is correct here, unlike in the Responses adapter: `n` defaults to 1
    # and this request never sets it, so there is exactly one choice and it is not
    # preceded by items of other types.
    first = choices[0]
    if not isinstance(first, dict):
        raise ClassifierError(
            "choices[0] is not an object",
            category=CATEGORY_NO_OUTPUT_TEXT,
            retryable=False,
        )

    message = first.get("message")
    if not isinstance(message, dict):
        raise ClassifierError(
            "choices[0] carried no `message` object",
            category=CATEGORY_NO_OUTPUT_TEXT,
            retryable=False,
        )

    return message, first.get("finish_reason")


def _reject_refusal(message: dict[str, Any]) -> None:
    """Checked before `finish_reason`, mirroring the Responses adapter.

    A refusal is a hard stop; a truncated refusal is still a refusal, and
    reporting it as truncation would send an operator after the token budget.
    """
    refusal = message.get("refusal")

    if isinstance(refusal, str) and refusal.strip():
        raise ClassifierError(
            "model refused the request",
            category=CATEGORY_REFUSAL,
            retryable=False,
        )


def _reject_truncated(finish_reason: object) -> None:
    if finish_reason == "length":
        # Not retryable: the same request with the same budget truncates again.
        raise ClassifierError(
            "response truncated at max_tokens",
            category=CATEGORY_INCOMPLETE_MAX_TOKENS,
            retryable=False,
        )

    if finish_reason == "content_filter":
        raise ClassifierError(
            f"response incomplete (finish_reason={clamp_token(finish_reason)})",
            category=CATEGORY_INCOMPLETE,
            retryable=False,
        )
