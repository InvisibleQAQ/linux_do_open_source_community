"""Test configuration.

Puts `backend/src` on `sys.path` so pure-domain modules import as
`linuxdo_oss.*` under plain CPython pytest, with no Worker runtime involved.

Modules that touch the Cloudflare runtime (`from workers import ...`, `import js`)
must never be imported from a pure test — they do not exist on CPython. That is
why the domain layer takes plain data and the runtime adapters live behind ports.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
