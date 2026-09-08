"""LLM wire protocols: the shapes of the request and the reply, nothing else.

Layer boundary, in the terms of `.trellis/spec/backend/directory-structure.md`:
this package is **pure logic over plain data**, so it may import the standard
library and nothing else. Not `workers`, not `js`, not `pydantic`, and never
`classifier` — the classifier dispatches into here, and an import back would be a
cycle as well as a pydantic dependency.

What lives here and what does not:

* here — how a request body is shaped, which header authenticates it, which path
  suffix reaches the endpoint, where the reply hides its structured output, and
  how each protocol announces a failure or a refusal.
* `classifier.py` — the classification itself: the prompt, the JSON Schema body,
  the `Decision` contract, and the `allowed_urls` gate that is the only thing
  actually protecting the database.

`__init__` re-exports the enums and the dispatch, deliberately NOT the three
adapter modules: `get_adapter` imports the one configured protocol lazily, and
eagerly importing all three here would undo that.

See `docs/adr/0005-llm-multi-protocol.md`.
"""

from __future__ import annotations

from linuxdo_oss.llm.errors import (
    CATEGORY_CONFIGURATION,
    CATEGORY_DUPLICATE_DECISION,
    CATEGORY_ENDPOINT_CONFIG,
    CATEGORY_INCOMPLETE,
    CATEGORY_INCOMPLETE_MAX_TOKENS,
    CATEGORY_MALFORMED_JSON,
    CATEGORY_NO_CANDIDATES,
    CATEGORY_NO_OUTPUT_TEXT,
    CATEGORY_PROVIDER_FAILED,
    CATEGORY_REFUSAL,
    CATEGORY_SCHEMA_VIOLATION,
    CATEGORY_TOO_MANY_CANDIDATES,
    CATEGORY_TRANSPORT,
    CATEGORY_UNKNOWN_REPOSITORY,
    ERROR_CATEGORIES,
    ClassifierError,
    clamp_token,
)
from linuxdo_oss.llm.protocol import (
    BASE_URL_FORBIDDEN_SUFFIXES,
    LLMProtocol,
    SchemaMode,
    WireAdapter,
    endpoint_url,
    get_adapter,
    parse_protocol,
    parse_schema_mode,
    validate_base_url,
)

__all__ = [
    "BASE_URL_FORBIDDEN_SUFFIXES",
    "CATEGORY_CONFIGURATION",
    "CATEGORY_DUPLICATE_DECISION",
    "CATEGORY_ENDPOINT_CONFIG",
    "CATEGORY_INCOMPLETE",
    "CATEGORY_INCOMPLETE_MAX_TOKENS",
    "CATEGORY_MALFORMED_JSON",
    "CATEGORY_NO_CANDIDATES",
    "CATEGORY_NO_OUTPUT_TEXT",
    "CATEGORY_PROVIDER_FAILED",
    "CATEGORY_REFUSAL",
    "CATEGORY_SCHEMA_VIOLATION",
    "CATEGORY_TOO_MANY_CANDIDATES",
    "CATEGORY_TRANSPORT",
    "CATEGORY_UNKNOWN_REPOSITORY",
    "ERROR_CATEGORIES",
    "ClassifierError",
    "LLMProtocol",
    "SchemaMode",
    "WireAdapter",
    "clamp_token",
    "endpoint_url",
    "get_adapter",
    "parse_protocol",
    "parse_schema_mode",
    "validate_base_url",
]
