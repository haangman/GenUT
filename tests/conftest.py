import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
SAMPLE = FIXTURES / "sample_project"
SYSINC = FIXTURES / "sysinc"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "genut_home"
    monkeypatch.setenv("GENUT_HOME", str(h))
    return h


@pytest.fixture
def fixtures():
    return FIXTURES


def write_compile_db(path, project=SAMPLE, sysinc=SYSINC, extra=None, compiler="cc"):
    """샘플 프로젝트용 compile_commands.json (절대 경로)."""
    entries = []
    for src in ("src/ring_buffer.c", "src/slot.c", "src/str_util.c"):
        entries.append(
            {
                "directory": str(project),
                "arguments": [compiler, "-Iinclude", "-isystem", str(sysinc), "-std=gnu11", *(extra or []), "-c", src, "-o", src + ".o"],
                "file": src,
            }
        )
    path.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    return path


@pytest.fixture
def sample_db(tmp_path):
    return write_compile_db(tmp_path / "compile_commands.json")


def pytest_collection_modifyitems(config, items):
    from genut.analysis.parser import libclang_available

    if not libclang_available():
        skip = pytest.mark.skip(reason="libclang 없음")
        for item in items:
            if "libclang" in item.keywords:
                item.add_marker(skip)
