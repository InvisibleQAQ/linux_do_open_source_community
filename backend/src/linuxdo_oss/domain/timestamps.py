"""The one timestamp format, in one place.

Every timestamp in D1 is TEXT holding ISO 8601 UTC with a trailing 'Z' and
millisecond precision:

    2026-08-31T15:18:11.000Z

That is not cosmetic. The schema compares timestamps with `<=` and orders by them
as TEXT (`retry_after <= ?`, `ORDER BY published_at DESC`), and keyset pagination
compares them across page boundaries. Those comparisons are only correct because
the format is fixed-width, zero-padded and always UTC. A single writer emitting
`2026-8-31T15:18:11Z` or a `+08:00` offset silently corrupts ordering — no error,
just wrong results.

So: nothing formats a timestamp by hand. Everything goes through `to_iso_utc`,
and anything arriving from outside is checked with `is_iso_utc`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

__all__ = ["ISO_UTC_FORMAT", "is_iso_utc", "parse_iso_utc", "to_iso_utc"]

ISO_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

# Anchored and exact-width on purpose: a permissive pattern defeats the point.
_ISO_UTC_RE = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\.\d{3}Z$"
)


def is_iso_utc(value: object) -> bool:
    """Whether `value` is a timestamp in the one accepted format."""
    return isinstance(value, str) and _ISO_UTC_RE.match(value) is not None


def to_iso_utc(moment: datetime) -> str:
    """Render a datetime in the canonical format.

    A naive datetime is rejected rather than assumed to be UTC: guessing is how a
    local-time value ends up stored as if it were UTC.
    """
    if moment.tzinfo is None:
        raise ValueError("refusing to format a naive datetime; attach a timezone")

    as_utc = moment.astimezone(UTC)

    # `%f` renders microseconds; the format is milliseconds, so trim the last 3.
    return as_utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{as_utc.microsecond // 1000:03d}Z"


def parse_iso_utc(value: str) -> datetime:
    """Parse a canonical timestamp. Raises ValueError on anything else."""
    if not is_iso_utc(value):
        raise ValueError(f"not an ISO 8601 UTC millisecond timestamp: {value!r}")

    return datetime.strptime(value, ISO_UTC_FORMAT).replace(tzinfo=UTC)
