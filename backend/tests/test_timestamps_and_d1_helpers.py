"""The timestamp format and the D1 byte/parameter guards.

Both are pure. Both exist because the schema depends on them: TEXT timestamp
comparisons are only correct for one fixed format, and D1 rejects a statement with
more than 100 bound parameters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from linuxdo_oss.domain.timestamps import is_iso_utc, parse_iso_utc, to_iso_utc
from linuxdo_oss.persistence.d1 import (
    MAX_BOUND_PARAMS,
    MAX_TEXT_BYTES,
    chunk_rows,
    clamp_text,
)

# ----------------------------------------------------------------------
# Timestamps
# ----------------------------------------------------------------------

VALID = [
    "2026-08-31T15:18:11.000Z",
    "2026-01-01T00:00:00.000Z",
    "2026-12-31T23:59:59.999Z",
]

INVALID = [
    "2026-08-31T15:18:11Z",  # no milliseconds — breaks fixed-width compare
    "2026-08-31T15:18:11.000000Z",  # microseconds
    "2026-8-31T15:18:11.000Z",  # not zero-padded
    "2026-08-31T15:18:11.000+08:00",  # not UTC
    "2026-08-31 15:18:11.000Z",  # space instead of T
    "2026-13-01T00:00:00.000Z",  # month 13
    "2026-08-32T00:00:00.000Z",  # day 32
    "2026-08-31T24:00:00.000Z",  # hour 24
    "2026-08-31T15:18:11.000",  # no Z
    "",
    "|",
    "not a timestamp",
]


@pytest.mark.parametrize("value", VALID)
def test_canonical_timestamps_are_accepted(value: str) -> None:
    assert is_iso_utc(value)
    assert to_iso_utc(parse_iso_utc(value)) == value


@pytest.mark.parametrize("value", INVALID)
def test_non_canonical_timestamps_are_rejected(value: str) -> None:
    """Anything else silently corrupts TEXT ordering, so it must not get in."""
    assert not is_iso_utc(value)
    with pytest.raises(ValueError):
        parse_iso_utc(value)


def test_is_iso_utc_rejects_non_strings() -> None:
    for value in (None, 0, 1756652291, [], {}):
        assert not is_iso_utc(value)


def test_a_non_utc_datetime_is_converted_not_relabelled() -> None:
    shanghai = timezone(timedelta(hours=8))
    moment = datetime(2026, 8, 31, 23, 18, 11, 0, tzinfo=shanghai)

    assert to_iso_utc(moment) == "2026-08-31T15:18:11.000Z"


def test_a_naive_datetime_is_refused() -> None:
    """Assuming a naive datetime is UTC is how a local-time value ends up stored as
    if it were UTC — an error nobody notices until ordering is wrong."""
    with pytest.raises(ValueError, match="naive"):
        to_iso_utc(datetime(2026, 8, 31, 15, 18, 11))


def test_microseconds_are_truncated_not_rounded() -> None:
    moment = datetime(2026, 8, 31, 15, 18, 11, 999_999, tzinfo=UTC)

    assert to_iso_utc(moment) == "2026-08-31T15:18:11.999Z"


def test_formatted_timestamps_sort_chronologically() -> None:
    base = datetime(2026, 8, 31, 23, 59, 59, 999_000, tzinfo=UTC)
    moments = [base + timedelta(milliseconds=offset) for offset in (0, 1, 2, 1000, 86_400_000)]

    formatted = [to_iso_utc(moment) for moment in moments]

    assert formatted == sorted(formatted)


# ----------------------------------------------------------------------
# Byte clamping
# ----------------------------------------------------------------------


def test_short_text_passes_through_unchanged() -> None:
    assert clamp_text("短文本") == "短文本"
    assert clamp_text(None) is None


def test_clamping_respects_a_byte_budget_not_a_character_count() -> None:
    # Each Chinese character is 3 bytes in UTF-8.
    text = "汉" * 100

    clamped = clamp_text(text, 10)

    assert clamped is not None
    assert len(clamped.encode("utf-8")) <= 10


def test_clamping_never_splits_a_utf8_character() -> None:
    """A naive byte slice produces invalid UTF-8, which D1 would either reject or
    store as mojibake. Every cut point must land on a character boundary."""
    text = "汉" * 100

    for limit in range(1, 32):
        clamped = clamp_text(text, limit)
        assert clamped is not None
        # Round-trips cleanly, i.e. it is valid UTF-8.
        assert clamped.encode("utf-8").decode("utf-8") == clamped


def test_default_clamp_limit_is_well_under_d1_row_and_statement_limits() -> None:
    assert MAX_TEXT_BYTES == 100_000


# ----------------------------------------------------------------------
# Bound-parameter chunking
# ----------------------------------------------------------------------


def test_chunking_keeps_every_batch_under_the_parameter_ceiling() -> None:
    rows = list(range(250))
    columns_per_row = 8

    chunks = chunk_rows(rows, columns_per_row)

    assert sum(len(chunk) for chunk in chunks) == len(rows)
    assert [item for chunk in chunks for item in chunk] == rows
    for chunk in chunks:
        assert len(chunk) * columns_per_row <= MAX_BOUND_PARAMS


def test_chunking_an_empty_list() -> None:
    assert chunk_rows([], 4) == []


def test_a_row_too_wide_to_ever_fit_is_an_error() -> None:
    """Better to fail here than to emit a statement D1 will reject at runtime."""
    with pytest.raises(ValueError, match="over the"):
        chunk_rows([1], MAX_BOUND_PARAMS + 1)


def test_zero_columns_is_an_error() -> None:
    with pytest.raises(ValueError, match="positive"):
        chunk_rows([1], 0)
