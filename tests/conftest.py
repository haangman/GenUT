import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "genut_home"
    monkeypatch.setenv("GENUT_HOME", str(h))
    return h


@pytest.fixture
def fixtures():
    return FIXTURES


def pytest_collection_modifyitems(config, items):
    from genut.analysis.parser import libclang_available

    if not libclang_available():
        skip = pytest.mark.skip(reason="libclang 없음")
        for item in items:
            if "libclang" in item.keywords:
                item.add_marker(skip)
