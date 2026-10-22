import json
import shutil
import subprocess
import sys
import tempfile
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


def toolchain_missing():
    """cmake·C++ 컴파일러·gtest 중 없는 것이 있으면 이유를, 다 있으면 None. gtest는 find_package로 확인한다."""
    cmake = shutil.which("cmake")
    if not cmake or not any(shutil.which(c) for c in ("c++", "g++", "clang++")):
        return "cmake 또는 C++ 컴파일러 없음"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        Path(d, "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.19)\nproject(probe CXX)\nfind_package(GTest REQUIRED)\n")
        cmd = [cmake, "-S", d, "-B", str(Path(d) / "b")] + (["-G", "Ninja"] if shutil.which("ninja") else [])
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as e:
            return f"cmake 실행 실패: {e}"
    return None if r.returncode == 0 else "gtest를 찾지 못함 (find_package(GTest))"


def pytest_collection_modifyitems(config, items):
    from genut.analysis.parser import libclang_available

    if not libclang_available():
        skip = pytest.mark.skip(reason="libclang 없음")
        for item in items:
            if "libclang" in item.keywords:
                item.add_marker(skip)
    integration = [item for item in items if "integration" in item.keywords]
    if integration:
        reason = toolchain_missing()
        if reason:
            for item in integration:
                item.add_marker(pytest.mark.skip(reason=reason))
