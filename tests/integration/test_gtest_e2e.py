"""샘플 프로젝트로 gtest 백엔드를 실제 cmake·컴파일러·gtest에서 끝까지 돌린다 (설계 13.2 통합).

cmake, C++ 컴파일러, gtest(find_package)가 없으면 conftest가 건너뛴다.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import SAMPLE
from genut.analysis.extractor import analyze_file
from genut.analysis.linkscope import judge_calls
from genut.analysis.model import AnalysisReport, FunctionInfo, Scope
from genut.backends.base import TestUnit
from genut.backends.gtest.backend import GTestBackend
from genut.backends.stub import NoopStubAdapter
from genut.config import GenutConfig
from genut.rules import Rules
from genut.state import FunctionState, TestCase
from genut.verify.classify import Classifier

pytestmark = [pytest.mark.integration, pytest.mark.libclang]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    shutil.copytree(SAMPLE, root)
    return root


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and "build-genut" not in p.relative_to(root).parts}


def _unit(be: GTestBackend, source: Path, name: str) -> TestUnit:
    """사전검증 뒤 전용 빌드의 compile_commands.json으로 분석하고 링크 범위를 판정한다."""
    fn = analyze_file(source, be.compile_db().find(source)).get(name)
    unit = TestUnit(source=source, report=AnalysisReport(function=fn), state=FunctionState(name=name, signature=fn.signature))
    unit.report.linkscope = judge_calls(fn, source, "gtest", Rules(), be.link_context(unit))
    return unit


def _placeholder(source: Path, name: str) -> TestUnit:
    fn = FunctionInfo(name=name, signature="", return_type="int", file=str(source), start_line=1, end_line=1)
    return TestUnit(source=source, report=AnalysisReport(function=fn), state=FunctionState(name=name))


def _case(name: str, body: str) -> TestCase:
    return TestCase(name=name, target_function="rb_push", body=body)


def test_suggested_link_target_leaves_undefined_symbol(project, tmp_path):
    """link_targets가 비면 File API로 대상 소스의 타깃(rb)만 링크한다. slot.c의 rb_alloc_slot은 정의 없음이 된다."""
    rb = project / "src" / "ring_buffer.c"
    be = GTestBackend(GenutConfig(), project)
    pf = be.preflight([_placeholder(rb, "rb_push")], tmp_path / "out")
    assert pf.ok, pf.log
    assert be.links == {"ring_buffer_rb_push": ["rb"]}
    unit = _unit(be, rb, "rb_push")
    scopes = {j.name: j.scope for j in unit.report.linkscope}
    assert scopes == {"rb_free_space": Scope.SAME_TU, "rb_alloc_slot": Scope.UNDEFINED, "memcpy": Scope.EXTERNAL}
    assert NoopStubAdapter().plan_stubs(unit.report.function, unit.report.linkscope, []).unsupported == ["rb_alloc_slot"]

    unit.state.cases = [_case("test_rb_push_null", 'EXPECT_EQ(rb_push(NULL, "a", 1), RB_EINVAL);')]
    out = be.build([be.render(unit)], tmp_path / "out")
    assert out.failed_functions == ["rb_push"]
    link = next(d for d in out.diagnostics["rb_push"] if d.stage == "link")
    assert "rb_alloc_slot" in link.message and link.owner == "@shared"
    assert Classifier().classify_diagnostic(link).strategy == "add_stub"


def test_build_run_crash_and_install(project, tmp_path):
    rb = project / "src" / "ring_buffer.c"
    before = _files(project)
    be = GTestBackend(GenutConfig.model_validate({"gtest": {"link_targets": ["rb", "rbslot"]}}), project)
    assert be.preflight([_placeholder(rb, "rb_push")], tmp_path / "out").ok
    unit = _unit(be, rb, "rb_push")
    assert {j.name: j.scope for j in unit.report.linkscope}["rb_alloc_slot"] == Scope.LINKABLE

    ok = 'struct rb rb;\nunsigned char buf[8];\nrb_init(&rb, buf, sizeof(buf));\nEXPECT_EQ(rb_push(&rb, "ab", 2), RB_OK);\nEXPECT_EQ(rb_used(&rb), 2u);'
    unit.state.cases = [
        _case("test_rb_push_ok", ok),
        _case("test_rb_push_full", 'struct rb rb;\nunsigned char buf[4];\nrb_init(&rb, buf, 4);\nEXPECT_EQ(rb_push(&rb, "abcdef", 6), RB_OK);'),
        _case("test_rb_push_bad_buf", 'struct rb rb = {};\nrb.buf = (unsigned char *)16;\nrb.cap = 8;\nrb_push(&rb, "a", 1);'),
        _case("test_rb_push_null", 'EXPECT_EQ(rb_push(NULL, "a", 1), RB_EINVAL);'),
        _case("test_rb_push_typo", 'EXPECT_EQ(rb_psh(NULL, "a", 1), RB_EINVAL);'),
    ]
    out = be.build([be.render(unit)], tmp_path / "r1")
    assert out.failed_functions == ["rb_push"]
    assert {d.owner for d in out.diagnostics["rb_push"] if d.severity == "error"} == {"test_rb_push_typo"}

    unit.state.cases.pop()  # 컴파일 오류 케이스를 빼고 다시
    r = be.render(unit)
    assert be.build([r], tmp_path / "r2").ok_functions == ["rb_push"]
    run = be.run(r, tmp_path / "r2")
    status = {k: v.status for k, v in run.results.items()}
    assert status == {"test_rb_push_ok": "pass", "test_rb_push_full": "fail", "test_rb_push_bad_buf": "crash", "test_rb_push_null": "not_run"}
    assert "-28" in run.results["test_rb_push_full"].observed  # 실제값(RB_ENOSPC)이 수정 단서로 남는다
    assert run.crash.suspect_case == "test_rb_push_bad_buf" and run.crash.category == "segv"
    if shutil.which("gdb"):
        assert run.crash.location.endswith("ring_buffer.c:39") and run.crash.chain[-1].startswith("rb_push")
    assert be.run(r, tmp_path / "r2", only=["test_rb_push_null"]).results["test_rb_push_null"].status == "pass"  # 격리 재실행

    # 최종 확인: 통과 케이스만으로 다시 빌드·실행하고 설치한다
    passed = [c for c in unit.state.cases if c.name in ("test_rb_push_ok", "test_rb_push_null")]
    final = be.render(unit, passed)
    assert be.build([final], tmp_path / "final").ok_functions == ["rb_push"]
    assert {v.status for v in be.run(final, tmp_path / "final").results.values()} == {"pass"}
    assert _files(project) == before  # 설치 전에는 프로덕트 트리에 아무것도 쓰지 않았다
    be.install(final)
    assert _files(project) - before == {"tests/generated/ring_buffer_rb_push_test.cc", "tests/generated/CMakeLists.txt"}

    # 사용자 빌드: 루트에 add_subdirectory 한 줄로 설치된 테스트가 빌드·실행된다
    lists = project / "CMakeLists.txt"
    lists.write_text(lists.read_text(encoding="utf-8") + "\nadd_subdirectory(tests/generated)\n", encoding="utf-8")
    user = tmp_path / "user-build"
    gen = ["-G", "Ninja"] if shutil.which("ninja") else []
    conf = subprocess.run(["cmake", "-S", str(project), "-B", str(user), *gen], capture_output=True, text=True)
    assert conf.returncode == 0, conf.stdout + conf.stderr
    build = subprocess.run(["cmake", "--build", str(user), "--target", "genut_ring_buffer_rb_push"], capture_output=True, text=True)
    assert build.returncode == 0, build.stdout + build.stderr
    exe = next(p for p in user.rglob("genut_ring_buffer_rb_push*") if p.is_file() and p.suffix in ("", ".exe"))
    res = subprocess.run([str(exe)], capture_output=True, text=True, cwd=tmp_path)
    assert res.returncode == 0 and "2 tests" in res.stdout, res.stdout

    # GenUT 전용 빌드는 루트가 설치본을 add_subdirectory 해도 작업 중 파일만 빌드한다 (타깃 이름 충돌 없음)
    assert be.configure().returncode == 0
