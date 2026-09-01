"""Public read API.

Exposes cursor-paginated topic collections. No internal processing fields, no
`uncertain` LLM decisions, no raw LLM output, no direct database access for
callers — see `schemas.py`.
"""
