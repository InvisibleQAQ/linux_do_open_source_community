"""The D1 boundary — the one place JavaScript objects become Python objects.

Every repository goes through these helpers and returns plain dicts or Pydantic
models. A `JsProxy` must never escape this module: Cloudflare's own `query-d1`
example hands a JsProxy straight to a JSON serializer and is marked
`@pytest.mark.xfail(reason="500 error, fixme")`, while their FastAPI example that
converts to real Python dicts passes its full suite.

Two facts that shape everything here:

  * **D1 has no interactive transactions.** Statements auto-commit. `batch()` is
    the only rollback boundary, so per-topic writes are one `batch()` call and
    claiming work is a single conditional UPDATE whose `meta.changes` you inspect.
  * **At most 100 bound parameters per statement.** Multi-row inserts must be
    chunked; `chunk_rows` does the arithmetic.

`db` in every signature is the D1 binding — `env.DB`. It is passed in rather than
looked up so the cron path (`scheduled` receives `env`) and the API path
(`request.scope["env"]`) can share one repository layer.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "MAX_BOUND_PARAMS",
    "MAX_TEXT_BYTES",
    "chunk_rows",
    "clamp_text",
    "execute",
    "query_all",
    "query_one",
    "row_to_dict",
    "rows_to_dicts",
]

# D1 rejects a statement with more than this many bound parameters.
MAX_BOUND_PARAMS = 100

# Applied to any text that comes from outside: cleaned post text, evidence
# excerpts, bounded error summaries, retained raw LLM output. Well under D1's
# 2 MB row and 100 KB statement limits, and it turns the PRD's unquantified
# "bounded size" into a number.
MAX_TEXT_BYTES = 100_000


def clamp_text(value: str | None, limit: int = MAX_TEXT_BYTES) -> str | None:
    """Truncate to a byte budget without splitting a UTF-8 character."""
    if value is None:
        return None

    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value

    return encoded[:limit].decode("utf-8", "ignore")


def chunk_rows(rows: list[Any], columns_per_row: int) -> list[list[Any]]:
    """Split rows into batches that fit under the bound-parameter ceiling."""
    if columns_per_row <= 0:
        raise ValueError("columns_per_row must be positive")

    per_chunk = MAX_BOUND_PARAMS // columns_per_row
    if per_chunk == 0:
        raise ValueError(
            f"a single row needs {columns_per_row} parameters, over the {MAX_BOUND_PARAMS} limit"
        )

    return [rows[i : i + per_chunk] for i in range(0, len(rows), per_chunk)]


def _check_params(params: tuple[Any, ...] | list[Any]) -> None:
    if len(params) > MAX_BOUND_PARAMS:
        raise ValueError(
            f"{len(params)} bound parameters exceeds D1's limit of {MAX_BOUND_PARAMS}; "
            "use chunk_rows()"
        )


def row_to_dict(row: Any) -> dict[str, Any] | None:
    """One D1 row as a Python dict. None passes through."""
    return None if row is None else row.to_py()


def rows_to_dicts(result: Any) -> list[dict[str, Any]]:
    """The `results` array of a D1 query as Python dicts."""
    return [row.to_py() for row in result.results]


async def query_all(db: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Run a SELECT and return every row as a dict.

    Python values are splatted into `bind(*params)` — pass a plain tuple. Do not
    reach for `pyodide.ffi.to_js`; D1's bind handles the conversion.
    """
    _check_params(params)
    return rows_to_dicts(await db.prepare(sql).bind(*params).all())


async def query_one(db: Any, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    """Run a SELECT and return the first row as a dict, or None."""
    _check_params(params)
    return row_to_dict(await db.prepare(sql).bind(*params).first())


async def execute(db: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    """Run a write and return its `meta`.

    `meta.changes` is how a conditional UPDATE reports whether it won the row —
    the basis of the claim lease, since there is no transaction to protect a
    read-then-write.
    """
    _check_params(params)
    return (await db.prepare(sql).bind(*params).run()).meta
