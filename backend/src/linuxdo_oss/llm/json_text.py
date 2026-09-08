"""Turning model-authored text into JSON, with exactly one mode-gated leniency.

Every protocol that carries its structured output as *text* needs this, so it
lives here once rather than three times.

The leniency: a model with no constrained decoding wraps JSON in a
```json fence often enough that refusing to unwrap one would make the degraded
Schema Modes unusable. Under `SchemaMode.STRICT` the fence is NOT unwrapped — a
fence there means the endpoint claimed strict schema support and did not deliver
it, and that must fail loudly instead of being smoothed over. Detecting exactly
that lie is most of the value of having the strict mode be explicit.

This is the only normalization applied to model output anywhere in the package.
It cannot turn a fabricated repository into an allowed one: the `allowed_urls`
re-check in `classifier.py` runs on the parsed result regardless of mode.
"""

from __future__ import annotations

import json
from typing import Any

from linuxdo_oss.llm.errors import CATEGORY_MALFORMED_JSON, ClassifierError
from linuxdo_oss.llm.protocol import SchemaMode

__all__ = ["loads_structured_json"]


def loads_structured_json(text: str, schema_mode: SchemaMode) -> Any:
    """Parse `text` as JSON, or raise a malformed-JSON `ClassifierError`."""
    try:
        return json.loads(text)
    except ValueError as error:
        if schema_mode is SchemaMode.STRICT:
            raise ClassifierError(
                "structured output is not valid JSON",
                category=CATEGORY_MALFORMED_JSON,
                retryable=False,
            ) from error

        unfenced = _strip_code_fence(text)
        if unfenced is None:
            raise ClassifierError(
                "structured output is not valid JSON",
                category=CATEGORY_MALFORMED_JSON,
                retryable=False,
            ) from error

        try:
            return json.loads(unfenced)
        except ValueError as inner:
            raise ClassifierError(
                "structured output is not valid JSON, fenced or unfenced",
                category=CATEGORY_MALFORMED_JSON,
                retryable=False,
            ) from inner


def _strip_code_fence(text: str) -> str | None:
    """The body of a single ``` fenced block, or None if `text` is not one.

    Deliberately narrow: the whole trimmed text must be one fenced block. It does
    not hunt for a JSON-looking substring inside prose, because "find the JSON in
    there somewhere" is a guess, and a guess about model output is how a partial
    result gets published.
    """
    stripped = text.strip()

    if not stripped.startswith("```") or not stripped.endswith("```"):
        return None

    inner = stripped[3:-3]

    # Drop the optional language tag on the opening fence (```json).
    newline = inner.find("\n")
    if newline == -1:
        return None

    first_line = inner[:newline].strip()
    if first_line and not first_line.isalnum():
        # Something other than a bare language tag sits on the fence line; this is
        # not the shape being unwrapped.
        return None

    return inner[newline + 1 :]
