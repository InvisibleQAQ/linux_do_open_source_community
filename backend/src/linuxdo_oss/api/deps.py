"""Request-scoped access to Worker bindings.

Inside a FastAPI route there is no `self.env`: the ASGI adapter injects the Worker
env into the ASGI scope instead. Cloudflare's own FastAPI example reads it as
`request.scope["env"]`.

Every route goes through these helpers so no route body reaches into `scope`
directly — that keeps exactly one place to change if the adapter's contract moves.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

__all__ = ["get_db", "get_env"]


def get_env(request: Request) -> Any:
    """The Worker env for this request.

    Attribute access only: `env.LLM_BASE_URL` works, `env["LLM_BASE_URL"]` raises.
    """
    env = request.scope.get("env")
    if env is None:
        raise RuntimeError(
            "no Worker env in the ASGI scope — this app must be served through "
            "workers.asgi, not a bare uvicorn"
        )
    return env


def get_db(request: Request) -> Any:
    """The D1 binding for this request.

    Name comes from `d1_databases[].binding` in wrangler.jsonc; renaming it there
    breaks this line.
    """
    return get_env(request).DB
