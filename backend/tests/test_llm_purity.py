"""The `llm/` package must import with nothing but the standard library.

Not a style rule. `workers` and `js` do not exist on CPython, so one runtime
import anywhere in this package's import graph makes the whole pure suite
unimportable — and pydantic is a module-level cost baked into the Worker's memory
snapshot against a 1 s startup limit.

This runs in a SUBPROCESS on purpose. Asserting `"pydantic" not in sys.modules`
inside pytest proves nothing: `test_classifier.py` imports pydantic in the same
interpreter, so the check would pass or fail depending on test order. A fresh
interpreter is the only place the import graph is observable.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

MODULES = [
    "linuxdo_oss.llm",
    "linuxdo_oss.llm.errors",
    "linuxdo_oss.llm.protocol",
    "linuxdo_oss.llm.json_text",
    "linuxdo_oss.llm.responses",
    "linuxdo_oss.llm.chat_completions",
    "linuxdo_oss.llm.anthropic",
]

FORBIDDEN = ("workers", "js", "pydantic", "httpx", "linuxdo_oss.classifier")

PROBE = """
import sys
sys.path.insert(0, {src!r})
import {module}
leaked = [name for name in {forbidden!r} if name in sys.modules]
print(",".join(leaked))
"""


@pytest.mark.parametrize("module", MODULES)
def test_the_module_imports_without_the_runtime_or_pydantic(module):
    probe = PROBE.format(src=str(SRC), module=module, forbidden=FORBIDDEN)

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    leaked = completed.stdout.strip()
    assert leaked == "", f"{module} pulled in forbidden modules: {leaked}"


def test_importing_the_package_does_not_import_the_three_adapters():
    """`get_adapter` imports lazily so a deployment pays for one protocol, not
    three, against the 1 s startup limit. Eagerly re-exporting them from
    `__init__` would undo that silently."""
    probe = PROBE.format(
        src=str(SRC),
        module="linuxdo_oss.llm",
        forbidden=(
            "linuxdo_oss.llm.responses",
            "linuxdo_oss.llm.chat_completions",
            "linuxdo_oss.llm.anthropic",
        ),
    )

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "", (
        f"`import linuxdo_oss.llm` eagerly imported: {completed.stdout.strip()}"
    )
