"""The failure vocabulary shared by the classifier and every wire adapter.

This module exists so `llm/*.py` can raise the same typed error the orchestrator
already classifies, without importing `classifier.py` — a wire adapter importing
the classifier that dispatches to it would be a cycle, and `classifier.py` pulls
in pydantic while everything here must stay stdlib-only.

`classifier.py` re-exports every name below, so existing callers and tests that do
`from linuxdo_oss.classifier import ClassifierError, CATEGORY_REFUSAL` keep
working unchanged. New code inside `llm/` imports from here.

Categories are the only thing about a failure that reaches a log or the
`sync_runs.error_summary` column, so they are short, fixed strings — never a
provider message, never a payload, never a key. `RunCounters.record_failure`
stores `topic_id:category`.
"""

from __future__ import annotations

__all__ = [
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
    "clamp_token",
]

# The request never left this process (bad base URL, missing key).
CATEGORY_CONFIGURATION = "configuration"
# The caller asked for a classification with nothing to classify.
CATEGORY_NO_CANDIDATES = "no_candidates"
# More distinct repositories than one bounded request should carry.
CATEGORY_TOO_MANY_CANDIDATES = "too_many_candidates"
# `fetch_text` raised: timeout, transport failure, or a 5xx/429. The retryable flag
# is taken from the raised error, not re-derived here.
CATEGORY_TRANSPORT = "transport"
# The endpoint answered, and the answer says the deployment is misconfigured:
# 401/403 (credential) or 404 (no such endpoint — almost always the configured
# LLM_PROTOCOL not being the protocol this endpoint speaks). Split out of
# CATEGORY_TRANSPORT because the two demand opposite operator responses and used
# to be indistinguishable in the log: retrying is pointless, editing config is not.
CATEGORY_ENDPOINT_CONFIG = "endpoint_config"
# The provider owns this failure, so it is worth retrying.
CATEGORY_PROVIDER_FAILED = "provider_failed"
# The reply was truncated by the output-token budget.
CATEGORY_INCOMPLETE_MAX_TOKENS = "incomplete_max_output_tokens"
# The reply stopped early for any other reason (e.g. content_filter).
CATEGORY_INCOMPLETE = "incomplete"
# The model refused the request.
CATEGORY_REFUSAL = "refusal"
# The reply carried no structured output where the configured protocol puts it —
# the endpoint is not shaped like the protocol it was configured as.
CATEGORY_NO_OUTPUT_TEXT = "no_output_text"
# The body, or the structured output inside it, is not JSON.
CATEGORY_MALFORMED_JSON = "malformed_json"
# Valid JSON that pydantic rejected: wrong shape, extra field, confidence out of
# [0, 1], or an `include` with no display name or summary.
CATEGORY_SCHEMA_VIOLATION = "schema_violation"
# The reason the gate exists: a repository that was not in the allowlist.
CATEGORY_UNKNOWN_REPOSITORY = "unknown_repository"
# Two decisions for one repository — the model contradicted itself.
CATEGORY_DUPLICATE_DECISION = "duplicate_decision"

ERROR_CATEGORIES = frozenset(
    {
        CATEGORY_CONFIGURATION,
        CATEGORY_NO_CANDIDATES,
        CATEGORY_TOO_MANY_CANDIDATES,
        CATEGORY_TRANSPORT,
        CATEGORY_ENDPOINT_CONFIG,
        CATEGORY_PROVIDER_FAILED,
        CATEGORY_INCOMPLETE_MAX_TOKENS,
        CATEGORY_INCOMPLETE,
        CATEGORY_REFUSAL,
        CATEGORY_NO_OUTPUT_TEXT,
        CATEGORY_MALFORMED_JSON,
        CATEGORY_SCHEMA_VIOLATION,
        CATEGORY_UNKNOWN_REPOSITORY,
        CATEGORY_DUPLICATE_DECISION,
    }
)


class ClassifierError(RuntimeError):
    """A classification attempt failed in a way the orchestrator must classify.

    `category` and `retryable` are required keyword arguments on purpose. The
    error-handling spec puts classification *on the exception*, decided where the
    cause is known — a caller looking at a `ClassifierError` cannot tell whether a
    5xx or a schema violation produced it, and defaulting either field would let a
    permanent validation failure be retried forever.
    """

    def __init__(self, message: str, *, category: str, retryable: bool) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable


def clamp_token(value: object, limit: int = 80) -> str:
    """Render a provider-supplied token for an error message, bounded.

    Every adapter needs this for the same reason: a `finish_reason`, `stop_reason`
    or error `code` comes from the provider and is unbounded until proven
    otherwise, and the logging spec forbids an unbounded value in a message.
    """
    return repr(str(value))[:limit]
