"""Runtime configuration, read from the Worker env.

The env object is attribute-access only: `env.LLM_BASE_URL` works,
`env["LLM_BASE_URL"]` raises, because the wrapper implements `__getattr__` and no
`__getitem__`. Getting this wrong fails at the first request, not at import.

Everything here is read once per invocation and passed down as a plain frozen
dataclass, so the domain and adapter layers never touch `env` directly and stay
testable under plain CPython.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from linuxdo_oss.llm import (
    LLMProtocol,
    SchemaMode,
    parse_protocol,
    parse_schema_mode,
    resolve_base_url,
)

logger = logging.getLogger(__name__)

# Hard platform limits that constrain these defaults. Do not raise them without
# re-reading the numbers in the root CLAUDE.md.
#
#   - At most 6 connections may simultaneously wait for response headers.
#   - Paid plan: 10,000 subrequests per invocation (Free: 50 — unusable here).
#   - Cron CPU 30 s (Paid), wall clock 15 min.
MAX_CONCURRENCY_CEILING = 6


@dataclass(frozen=True, slots=True)
class Settings:
    channel_feed_url: str
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_protocol: LLMProtocol
    llm_schema_mode: SchemaMode
    llm_prompt_version: str
    sync_batch_size: int
    http_timeout_seconds: float
    max_response_bytes: int
    max_concurrency: int


def _required(env: object, name: str) -> str:
    value = getattr(env, name, None)
    if not value:
        raise RuntimeError(f"missing required configuration: {name}")
    return str(value)


# PEP 695 syntax here, unlike `api/schemas.py`'s classic `Generic[T]`: that one is
# a pydantic model and the runtime pins pydantic 2.10.6 (hence the `UP046` ignore
# in pyproject.toml). This is a plain function, so the constraint does not apply.
def _number[N: (int, float)](env: object, name: str, default: N, cast: Callable[[Any], N]) -> N:
    """Read a numeric setting, or raise `RuntimeError`. Never `ValueError`.

    `int("twenty")` raises `ValueError`, and `run_sync` wraps `load_settings` in
    `except RuntimeError` only — so before this existed a typo in
    `SYNC_BATCH_SIZE` escaped a function that promises never to raise and took the
    cron invocation down with it. `.env` values are always strings and
    `wrangler.jsonc` `vars` are hand-edited JSON, so this is a typo away, not
    hypothetical.

    The message names the variable and NOT its value: `int`'s own message quotes
    the input, and this one reaches the log through `run_sync`'s
    `logger.exception`.

    Falsy is the pre-existing contract, kept deliberately: an absent, blank or
    zero value means "use the default", exactly as the old `or default` did.
    """
    raw = getattr(env, name, None)

    if not raw:
        return default

    try:
        return cast(raw)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{name} must be a number") from error


def load_settings(env: object) -> Settings:
    """Build Settings from the Worker env. Raises `RuntimeError`, never `ValueError`.

    Fails loudly on a missing value rather than defaulting, because a silently
    empty `LLM_BASE_URL` would turn into a confusing HTTP error deep in the
    classifier instead of an obvious configuration problem here.

    `LLM_PROTOCOL` and `LLM_SCHEMA_MODE` are the same bargain one level up: both
    default when absent (to `responses` / `strict`, which is what the deployment
    did before either existed) and both refuse to guess when present and wrong.
    """
    base_url = _required(env, "LLM_BASE_URL").rstrip("/")

    if not base_url.startswith("https://"):
        # PRD security rule: the API key is only ever sent to a configured HTTPS
        # origin, and that origin is never accepted from a public request.
        raise RuntimeError("LLM_BASE_URL must be an https:// URL from deployment config")

    # `llm/` signals a bad value with ValueError, but every configuration failure
    # must leave this module as RuntimeError: that is the type the error-handling
    # spec assigns to the third kind of failure, and the only one `run_sync`
    # catches around `load_settings`. A ValueError escaping here would propagate
    # out of `run_sync`, which promises never to raise.
    try:
        # Both parsers raise on an unrecognised value instead of defaulting. A
        # typo in LLM_PROTOCOL that quietly became `responses` would surface as
        # 404s from an endpoint the operator believed they had selected — the
        # exact confusion this configuration surface exists to remove.
        protocol = parse_protocol(getattr(env, "LLM_PROTOCOL", None) or LLMProtocol.RESPONSES.value)
        schema_mode = parse_schema_mode(
            getattr(env, "LLM_SCHEMA_MODE", None) or SchemaMode.STRICT.value
        )

        # The path suffix is the adapter's to append, so the root must not already
        # carry one. A trailing copy of *this* protocol's own endpoint path is
        # stripped rather than refused: pasting the endpoint URL out of a provider's
        # documentation is the ordinary way this value gets configured. An ending
        # owned by another protocol stays a hard failure, because there the wrong
        # value is LLM_PROTOCOL and stripping it would hide that behind a 404 five
        # minutes later, from a cron run. `resolve_base_url` names the offending
        # suffix and never the URL — a gateway base URL can carry a key in a query
        # string. See docs/adr/0006-llm-base-url-normalization.md.
        root = resolve_base_url(protocol, base_url)
    except ValueError as error:
        raise RuntimeError(str(error)) from error

    if root != base_url:
        # The result is always a prefix of the input, so the remainder IS the
        # suffix that went away — which is why `resolve_base_url` needs no second
        # return value. Logged because a tolerated paste is still a configuration
        # the operator should see named; the suffix comes from ENDPOINT_SUFFIX, a
        # bounded set, and the URL itself never appears.
        logger.warning(
            "LLM_BASE_URL carried the %r endpoint path of protocol %s; using the API root",
            base_url[len(root) :],
            protocol.value,
        )

    if schema_mode is not SchemaMode.STRICT:
        # Degrading is legitimate and configured, never silent: ADR 0005 forbids
        # the runtime fallback, not the deployment choice. Every run says so, and
        # only the mode name is logged — never a URL, model or key.
        logger.warning(
            "LLM_SCHEMA_MODE=%s: the canonical_url enum is not enforced by the "
            "endpoint; the allowlist re-check is the only guard",
            schema_mode.value,
        )

    batch_size = _number(env, "SYNC_BATCH_SIZE", 20, int)
    concurrency = min(_number(env, "SYNC_CONCURRENCY", 4, int), MAX_CONCURRENCY_CEILING)

    return Settings(
        channel_feed_url=_required(env, "CHANNEL_FEED_URL"),
        llm_base_url=root,
        llm_model=_required(env, "LLM_MODEL"),
        llm_api_key=_required(env, "LLM_API_KEY"),
        llm_protocol=protocol,
        llm_schema_mode=schema_mode,
        llm_prompt_version=str(getattr(env, "LLM_PROMPT_VERSION", "1") or "1"),
        sync_batch_size=batch_size,
        http_timeout_seconds=_number(env, "HTTP_TIMEOUT_SECONDS", 10.0, float),
        max_response_bytes=_number(env, "MAX_RESPONSE_BYTES", 2 * 1024 * 1024, int),
        max_concurrency=concurrency,
    )
