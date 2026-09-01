"""Pure domain logic.

Nothing in this package may import from the Cloudflare runtime (`workers`, `js`,
`pyodide`), from `httpx`, or from the persistence layer. Everything here must be
importable and testable under plain CPython.
"""
