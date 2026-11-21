"""A guard against testing the wrong ``calotype``.

The service in this repository is also a package named ``calotype`` (at the
repository root). These tests are for the client in ``clients/python/src``;
if the import resolves anywhere else, stop before a single test runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import calotype

_SRC = Path(__file__).resolve().parents[1] / "src" / "calotype"
_FOUND = Path(calotype.__file__).resolve().parent
if not hasattr(calotype, "Calotype") or (_FOUND != _SRC and "site-packages" not in _FOUND.parts):
    raise pytest.UsageError(
        f"'import calotype' found {_FOUND}, not the client in {_SRC}. "
        "Run the tests from clients/python with the client installed: uv run pytest"
    )
