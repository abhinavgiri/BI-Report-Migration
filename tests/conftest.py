"""Shared fixtures.

The real .pbix files in input/ ARE the test fixtures -- we deliberately test
against actual Power BI output rather than hand-written samples, so the parser
can never drift away from reality.
"""
import pathlib

import pytest

INPUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "input"

RETAIL = INPUT_DIR / "Retail Analysis Sample PBIX.pbix"
REGIONAL = INPUT_DIR / "Regional Sales Sample.pbix"


def _require(path):
    if not path.exists():
        pytest.skip("sample file missing: " + path.name)
    return path


@pytest.fixture(scope="session")
def retail_pbix():
    return _require(RETAIL)


@pytest.fixture(scope="session")
def regional_pbix():
    return _require(REGIONAL)
