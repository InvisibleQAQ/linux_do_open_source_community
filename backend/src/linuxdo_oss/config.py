"""Runtime configuration, read from the Worker env.

The env object is attribute-access only: `env.LLM_BASE_URL` works,
`env["LLM_BASE_URL"]` raises, because the wrapper implements `__getattr__` and no
`__getitem__`. Getting this wrong fails at the first request, not at import.

Everything here is read once per invocation and passed down as a plain frozen
dataclass, so the domain and adapter layers never touch `env` directly and stay
testable under plain CPython.
"""

from __future__ import annotations

from dataclasses import dataclass

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


def load_settings(env: object) -> Settings:
    """Build Settings from the Worker env.

    Fails loudly on a missing value rather than defaulting, because a silently
    empty `LLM_BASE_URL` would turn into a confusing HTTP error deep in the
    classifier instead of an obvious configuration problem here.
    """
    base_url = _required(env, "LLM_BASE_URL").rstrip("/")

    if not base_url.startswith("https://"):
        # PRD security rule: the API key is only ever sent to a configured HTTPS
        # origin, and that origin is never accepted from a public request.
        raise RuntimeError("LLM_BASE_URL must be an https:// URL from deployment config")

    batch_size = int(getattr(env, "SYNC_BATCH_SIZE", 20) or 20)
    concurrency = min(int(getattr(env, "SYNC_CONCURRENCY", 4) or 4), MAX_CONCURRENCY_CEILING)

    return Settings(
        channel_feed_url=_required(env, "CHANNEL_FEED_URL"),
        llm_base_url=base_url,
        llm_model=_required(env, "LLM_MODEL"),
        llm_api_key=_required(env, "LLM_API_KEY"),
        llm_prompt_version=str(getattr(env, "LLM_PROMPT_VERSION", "1") or "1"),
        sync_batch_size=batch_size,
        http_timeout_seconds=float(getattr(env, "HTTP_TIMEOUT_SECONDS", 10) or 10),
        max_response_bytes=int(
            getattr(env, "MAX_RESPONSE_BYTES", 2 * 1024 * 1024) or 2 * 1024 * 1024
        ),
        max_concurrency=concurrency,
    )
