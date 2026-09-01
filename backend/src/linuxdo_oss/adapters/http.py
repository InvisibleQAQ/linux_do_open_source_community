"""The single outbound HTTP egress point.

Every external request — both RSS readers and the LLM adapter — goes through
`fetch_text`. The PRD requires a timeout and a response-size bound on all of
them; one implementation is the only way that stays true.

Why `from workers import fetch` and not httpx: httpx's sync client blocks the
isolate, and every module-level import is executed at deploy time and baked into
the memory snapshot against a 1 second Worker startup limit. The runtime's own
fetch costs nothing extra.

`[UNKNOWN]` — `workers.fetch` exposes no timeout option. Two mechanisms are
stacked here:

  1. `signal=AbortSignal.timeout(ms)`. Kwargs are forwarded verbatim into the JS
     `RequestInit`, and `AbortSignal.timeout` exists in workerd — but no
     Cloudflare doc or example shows it from Python, and it is not in the typed
     kwargs. **This must be verified on a DEPLOYED Worker** before the readers
     rely on it (see spikes/egress).
  2. `asyncio.wait_for` as an outer bound, which holds regardless of whether (1)
     works.

If (1) turns out to be a no-op, (2) still bounds wall clock; the request itself
would keep running until the isolate ends, which is acceptable but wasteful. Do
not remove (2) on the assumption that (1) works.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from workers import fetch as worker_fetch

__all__ = ["FetchError", "FetchResult", "fetch_text"]


class FetchError(RuntimeError):
    """An outbound request failed in a way the caller must handle.

    Carries `status` (0 for transport failures and timeouts) and `retryable`, so
    the orchestrator can distinguish "try again next Cron run" from "this will
    never work".
    """

    def __init__(self, message: str, *, status: int = 0, retryable: bool = True) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class FetchResult:
    status: int
    content_type: str
    text: str


async def fetch_text(
    url: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
) -> FetchResult:
    """Fetch `url` and return its body as text, bounded in time and size.

    Raises FetchError on any non-2xx status, on timeout, and when the response
    exceeds `max_bytes`.
    """
    request_headers = {"Accept": "*/*", **(headers or {})}

    try:
        response = await asyncio.wait_for(
            _fetch(url, method, request_headers, body, timeout_seconds),
            timeout=timeout_seconds,
        )
    except TimeoutError as error:
        raise FetchError(f"timeout after {timeout_seconds}s", status=0, retryable=True) from error

    status = int(response.status)

    if status >= 400:
        # 4xx is a permanent problem with the request; 5xx and 429 are worth
        # retrying on a later Cron run.
        raise FetchError(
            f"upstream returned {status}",
            status=status,
            retryable=status >= 500 or status == 429,
        )

    declared = response.headers.get("content-length")
    if declared and int(declared) > max_bytes:
        raise FetchError(
            f"content-length {declared} exceeds cap {max_bytes}",
            status=status,
            retryable=False,
        )

    text = await _read_bounded_text(response, max_bytes)

    return FetchResult(
        status=status,
        content_type=response.headers.get("content-type") or "",
        text=text,
    )


async def _fetch(
    url: str,
    method: str,
    headers: dict[str, str],
    body: str | None,
    timeout_seconds: float,
):
    kwargs: dict[str, object] = {"method": method, "headers": headers}

    if body is not None:
        kwargs["body"] = body

    signal = _abort_signal(timeout_seconds)
    if signal is not None:
        kwargs["signal"] = signal

    return await worker_fetch(url, **kwargs)


def _abort_signal(timeout_seconds: float):
    """Best-effort JS-level timeout. Returns None when unavailable.

    Import is local, not module level: `js` is a runtime-only module and importing
    it at module scope would make this file unimportable under plain CPython
    pytest.
    """
    try:
        from js import AbortSignal
    except ImportError:
        return None

    try:
        return AbortSignal.timeout(int(timeout_seconds * 1000))
    except (AttributeError, TypeError):
        return None


async def _read_bounded_text(response, max_bytes: int) -> str:
    """Read the body, refusing to buffer more than `max_bytes`.

    A missing or lying `content-length` is normal, so the stream is read
    defensively against a byte budget and cancelled on overflow rather than
    trusting the header.
    """
    stream = getattr(response, "body", None)

    if stream is None:
        # No stream to walk — fall back to a full read, then enforce the cap.
        text = await response.text()
        if len(text.encode("utf-8")) > max_bytes:
            raise FetchError(f"response exceeds cap {max_bytes}", retryable=False)
        return text

    reader = stream.getReader()
    chunks: list[bytes] = []
    total = 0

    try:
        while True:
            chunk = await reader.read()
            if chunk.done:
                break

            data = bytes(chunk.value.to_py())
            total += len(data)

            if total > max_bytes:
                raise FetchError(f"response exceeds cap {max_bytes}", retryable=False)

            chunks.append(data)
    finally:
        # Cancelling an already-finished reader is not an error worth surfacing.
        with contextlib.suppress(Exception):
            await reader.cancel()

    return b"".join(chunks).decode("utf-8", "replace")
