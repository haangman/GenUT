"""KUnit 백엔드 (설계 13.1 9단계): 뼈대·집합 파일, 스크립트 계약, 가짜 스크립트로 빌드·실행·격리·커버리지·복원, 로그 픽스처."""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from conftest import FIXTURES
from genut.analysis.model import AnalysisReport, FunctionInfo, GlobalRef
from genut.backends.base import CommandResult, TestUnit
from genut.backends.kunit import skeleton
from genut.backends.kunit.backend import KUnitBackend
from genut.backends.kunit.script import KUnitScript
from genut.config import GenutConfig
from genut.context import make_context
from genut.coverage import function_coverage
from genut.llm.fake_provider import FakeProvider
from genut.orchestrator import GenOptions, Orchestrator
from genut.paths import ensure_home_layout
from genut.products import add_product
from genut.state import CaseStatus, FunctionState, SharedArea, TestCase
from genut.verify.logs.crash import CrashContext, build_brief, make_source_reader
from genut.verify.logs.ktap import parse_ktap, strip_timestamps
from genut.verify.logs.reduce import reduce_kunit_log

TREE = FIXTURES / "kernel_tree"
FAKE = FIXTURES / "kunit" / "fake_kunit.py"
LOGS = FIXTURES / "logs"
FN = dict(name="foo_clamp", signature="int foo_clamp(int v, int lo, int hi)", return_type="int", start_line=4, end_line=13,
          includes=['#include "foo.h"', "#include <linux/errno.h>"])


def _unit(tree: Path, cases=(), **fn) -> TestUnit:
    src = tree / "drivers" / "foo" / "foo.c"
    info = FunctionInfo(file=str(src), **{**FN, **fn})
    state = FunctionState(name=info.name, cases=[TestCase(name=n, target_function=info.name, body=b) for n, b in cases])
    return TestUnit(source=src, report=AnalysisReport(function=info), state=state)


# ------------------------------------------------------------------ 뼈대
def test_function_file_layout():
    g = [GlobalRef(name="foo_count", access="write", decl_file=str(TREE / "drivers" / "foo" / "foo.h"))]
    u = _unit(TREE, [("test_foo_clamp_mid", "struct x *p = make_x();\nKUNIT_EXPECT_EQ(test, use_x(p), 5);")], globals=g)
    u.state.shared = SharedArea(helpers={"make_x": "struct x *make_x(void) { return NULL; }", "use_x": "static int use_x(struct x *p) { return foo_clamp(make_x() ? 1 : 5, 0, 9); }",
                                         "unused": "static void unused(void) {}"}, setup_code="setup();")
    r = skeleton.render(u, "foo_foo_clamp_test.c", file_dir=TREE / "drivers" / "foo" / "tests")
    t = r.text
    assert t.splitlines()[1:3] == ['#include "../foo.h"', "#include <linux/errno.h>"]  # 함수별 파일 위치 기준 경로
    assert "static struct x *foo_clamp__make_x(void)" in t and "static int foo_clamp__use_x(struct x *p) { return foo_clamp(foo_clamp__make_x()" in t
    assert "struct x *p = foo_clamp__make_x();" in t and "unused" not in t  # 한 번역 단위라 헬퍼에 static과 함수 접두어
    assert "static unsigned char genut_saved_foo_clamp_foo_count[sizeof(foo_count)];" in t
    assert "\tmemcpy(genut_saved_foo_clamp_foo_count, &foo_count, sizeof(foo_count));\n\tsetup();\n\treturn 0;" in t
    assert "\tKUNIT_CASE(test_foo_clamp_mid),\n\t{}\n};" in t and '\t.name = "foo_foo_clamp_test",' in t
    s, e = r.line_map.cases["test_foo_clamp_mid"]
    lines = t.splitlines()
    assert lines[s - 1] == "static void test_foo_clamp_mid(struct kunit *test)" and lines[e - 1] == "}"
    assert r.suite == "foo_foo_clamp_test" and skeleton.suite_of_file(t) == "foo_foo_clamp_test"
    empty = skeleton.render(u, "x.c", [])
    assert "static struct kunit_case foo_foo_clamp_test_cases[] = {\n\t{}\n};" in empty.text


def test_aggregate_file():
    agg = TREE / "drivers" / "foo" / "foo_kunit.c"
    text = skeleton.aggregate(agg, [(TREE / "drivers" / "foo" / "tests" / "a_test.c", "a_test"), (TREE / "lib" / "b_test.c", "b_test")],
                              sources=[TREE / "drivers" / "foo" / "foo.c"])
    lines = text.splitlines()
    assert lines[1:3] == ["#include <kunit/test.h>", "#include <linux/string.h>"]
    assert '#include "foo.c"' in lines and '#include "tests/a_test.c"' in lines and '#include "../../lib/b_test.c"' in lines
    assert lines[-1] == "kunit_test_suites(&a_test, &b_test);"


# ------------------------------------------------------------- 스크립트 계약
def test_script_command_and_result_categories(tmp_path):
    calls = []

    def runner(cmd, cwd=None, timeout=None, env=None):
        calls.append(cmd)
        return CommandResult(cmd, runner.rc, "stdout", 1.0, timed_out=runner.timed_out)

    runner.rc, runner.timed_out = 0, False
    s = KUnitScript(tmp_path / "run.py", tmp_path / "foo_kunit.c", 60, runner)
    res = s("all", tmp_path / "log" / "k.log", filter_="suite.case", coverage_out=tmp_path / "cov")
    assert calls[-1] == [sys.executable, str(tmp_path / "run.py"), str(tmp_path / "foo_kunit.c"), "--mode", "all", "--log", str(tmp_path / "log" / "k.log"),
                         "--filter", "suite.case", "--coverage-out", str(tmp_path / "cov")]
    assert res.category == "ok" and res.built and res.log == "stdout"  # 로그 파일이 없으면 stdout
    for rc, cat, built in ((10, "build_failed", False), (20, "run_failed", True), (3, "script_error", False)):
        runner.rc = rc
        r = s("build", tmp_path / "k.log")
        assert (r.category, r.built) == (cat, built)
    runner.timed_out = True
    assert s("run", tmp_path / "k.log").category == "timeout"
    sh = KUnitScript(tmp_path / "run.sh", tmp_path / "t.c", 1, runner)
    assert sh.command()[-1] == str(tmp_path / "run.sh") and (len(sh.command()) == 2) == (os.name == "nt")


# ------------------------------------------------- 가짜 스크립트로 백엔드
@pytest.fixture
def kern(tmp_path, monkeypatch):
    tree = tmp_path / "linux"
    shutil.copytree(TREE, tree)
    db = [{"directory": str(tree), "file": "drivers/foo/foo.c",
           "arguments": ["gcc", "-nostdinc", "-isystem", str(tree / "include"), "-D__KERNEL__", "-std=gnu11", "-c", "-o", "drivers/foo/foo.o", "drivers/foo/foo.c"]}]
    (tree / "compile_commands.json").write_text(json.dumps(db), encoding="utf-8")
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_KUNIT_CALLS", str(calls))

    def make(**kunit):
        cfg = GenutConfig.model_validate({"product": {"backend": "kunit"}, "coverage": {"enabled": True},
                                          "kunit": {"test_file": "drivers/foo/foo_kunit.c", "test_dir": "drivers/foo/tests", "run_script": str(FAKE), **kunit}})
        return KUnitBackend(cfg, tree)

    def history():
        return [json.loads(x) for x in calls.read_text(encoding="utf-8").splitlines()] if calls.exists() else []

    return tree, make, history


def _modes(history):
    return [h[h.index("--mode") + 1] for h in history()]


def test_preflight_build_run_and_coverage(kern, tmp_path):
    tree, make, history = kern
    be = make()
    u = _unit(tree, [("test_foo_clamp_mid", "KUNIT_EXPECT_EQ(test, foo_clamp(5, 0, 9), 5); // COVERS=4,6,8,10,12"),
                     ("test_foo_clamp_low", "KUNIT_EXPECT_EQ(test, foo_clamp(-1, 0, 9), 1); // FAIL ACTUAL=0 COVERS=4,6,8,9"),
                     ("test_foo_clamp_null", "KUNIT_EXPECT_EQ(test, foo_clamp(0, 0, 0), 0); // CRASH"),
                     ("test_foo_clamp_high", "KUNIT_EXPECT_EQ(test, foo_clamp(20, 0, 9), 9);")])
    assert be.configure([u]).returncode == 0
    assert be.preflight([u], tmp_path / "pf").ok and _modes(history) == ["build"]  # 사전검증은 빌드만
    r = be.render(u)
    out = tmp_path / "r1"
    b = be.build([r], out)
    assert b.ok_functions == ["foo_clamp"] and _modes(history)[-1] == "all"  # 빌드와 실행을 한 번에
    assert history()[-1][-2:] == ["--coverage-out", str(out / "coverage")]
    agg = (tree / "drivers" / "foo" / "foo_kunit.c").read_text(encoding="utf-8")
    assert '#include "tests/foo_foo_clamp_test.c"' in agg and "kunit_test_suites(&foo_foo_clamp_test);" in agg
    rr = be.run(r, out)
    assert len(history()) == 2  # 실행은 build의 결과를 꺼내 쓴다
    st = {k: v.status for k, v in rr.results.items()}
    assert st == {"test_foo_clamp_mid": "pass", "test_foo_clamp_low": "fail", "test_foo_clamp_null": "crash", "test_foo_clamp_high": "not_run"}
    low = rr.results["test_foo_clamp_low"]
    assert low.observed == ["0"] and low.message == "같지 않음: foo_clamp(-1, 0, 9) [0] ≠ 1" and (low.expected, low.actual) == ("1", "0")
    assert rr.crash.category == "null_deref" and rr.crash.suspect_case == "test_foo_clamp_null" and rr.crash.chain[-1].startswith("foo_clamp")
    reduced = (out / "foo_clamp.run.reduced.md").read_text(encoding="utf-8")
    assert "### 우리 스위트 결과" in reduced and "### 크래시 브리프" in reduced and "RAX:" not in reduced
    cov = function_coverage(be.coverage(r, ["test_foo_clamp_mid"], out), u.report.function)
    assert (cov.covered, cov.total, cov.uncovered_lines) == (6, 8, [7, 11])


def test_build_failure_is_attributed_to_the_case(kern, tmp_path):
    tree, make, _ = kern
    be = make()
    u = _unit(tree, [("test_foo_clamp_ok", "KUNIT_EXPECT_EQ(test, foo_clamp(1, 0, 2), 1);"), ("test_foo_clamp_typo", "KUNIT_EXPECT_EQ(test, nope, 1); // COMPILE_ERROR")])
    be.configure([u])
    r = be.render(u)
    b = be.build([r], tmp_path / "b")
    assert b.failed_functions == ["foo_clamp"] and [d.owner for d in b.diagnostics["foo_clamp"]] == ["test_foo_clamp_typo"]
    assert be.run(r).results["test_foo_clamp_ok"].status == "not_run"  # 빌드가 깨지면 실행 결과가 없다


def test_timeout_and_kasan(kern, tmp_path):
    tree, make, _ = kern
    be = make()
    u = _unit(tree, [("test_foo_clamp_loop", "for (;;); // TIMEOUT"), ("test_foo_clamp_freed", "KUNIT_EXPECT_EQ(test, foo_clamp(1, 0, 2), 1); // KASAN"),
                     ("test_foo_clamp_ok", "KUNIT_EXPECT_EQ(test, foo_clamp(1, 0, 2), 1);")])
    be.configure([u])
    r = be.render(u)
    be.build([r], tmp_path / "b")
    rr = be.run(r, tmp_path / "b")
    assert {k: v.status for k, v in rr.results.items()} == {"test_foo_clamp_loop": "timeout", "test_foo_clamp_freed": "crash", "test_foo_clamp_ok": "pass"}
    assert rr.crash.category == "kasan_uaf" and rr.crash.suspect_case == "test_foo_clamp_freed"


@pytest.mark.parametrize("supports_filter", [True, False])
def test_isolation_uses_filter_when_supported(kern, tmp_path, supports_filter):
    tree, make, history = kern
    be = make(script_supports_filter=supports_filter)
    u = _unit(tree, [("test_foo_clamp_a", "KUNIT_EXPECT_EQ(test, foo_clamp(1, 0, 2), 1);"), ("test_foo_clamp_b", "KUNIT_EXPECT_EQ(test, foo_clamp(1, 0, 2), 1); // CRASH")])
    be.configure([u])
    r = be.render(u)
    be.build([r], tmp_path / "b")
    iso = be.run(r, tmp_path / "b", only=["test_foo_clamp_b"])
    assert iso.results["test_foo_clamp_b"].status == "crash" and list(iso.results) == ["test_foo_clamp_b"]
    last = history()[-1]
    if supports_filter:
        assert last[last.index("--mode") + 1] == "run" and last[last.index("--filter") + 1] == "foo_foo_clamp_test.test_foo_clamp_b"
    else:  # 필터가 없으면 용의자만 담은 파일로 다시 빌드·실행
        assert last[last.index("--mode") + 1] == "all" and "--filter" not in last
        assert "test_foo_clamp_a" not in (tree / "drivers" / "foo" / "tests" / "foo_foo_clamp_test.c").read_text(encoding="utf-8")


def test_install_keeps_previous_files_and_finish_restores(kern, tmp_path):
    tree, make, _ = kern
    tests = tree / "drivers" / "foo" / "tests"
    tests.mkdir()
    (tests / "foo_old_test.c").write_text('static struct kunit_suite foo_old_test = {\n\t.name = "foo_old_test",\n};\n', encoding="utf-8")
    agg = tree / "drivers" / "foo" / "foo_kunit.c"
    agg.write_text("/* 사용자 원본 */\n", encoding="utf-8")
    be = make()
    u = _unit(tree, [("test_foo_clamp_a", "KUNIT_EXPECT_EQ(test, foo_clamp(1, 0, 2), 1);")])
    be.configure([u])
    be.build([be.render(u)], tmp_path / "b")
    assert "foo_old_test" not in agg.read_text(encoding="utf-8")  # 작업 중에는 이번 실행의 파일만
    be.install(be.render(u))
    be.finish()
    text = agg.read_text(encoding="utf-8")
    assert "kunit_test_suites(&foo_foo_clamp_test, &foo_old_test);" in text and (tests / "foo_foo_clamp_test.c").exists()

    be2 = make()  # 아무것도 설치하지 못한 실행은 커널 트리를 원래대로 되돌린다
    u2 = _unit(tree, [("test_foo_clamp_b", "KUNIT_EXPECT_EQ(test, foo_clamp(1, 0, 2), 1);")], name="foo_other")
    be2.configure([u2])
    be2.build([be2.render(u2)], tmp_path / "b2")
    assert (tests / "foo_foo_other_test.c").exists()
    be2.finish()
    assert not (tests / "foo_foo_other_test.c").exists() and agg.read_text(encoding="utf-8") == text


def test_link_context_and_cmd_fallback(kern):
    tree, make, _ = kern
    be = make()
    u = _unit(tree)
    assert be.link_context(u).test_kind == "built-in"
    cfg = tree / ".config"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("CONFIG_FOO_KUNIT_TEST=y", "CONFIG_FOO_KUNIT_TEST=m"), encoding="utf-8")
    assert be.link_context(u).test_kind == "module"  # 모듈이면 EXPORT_SYMBOL만 링크된다
    (tree / "compile_commands.json").unlink()
    (tree / "drivers" / "foo" / ".foo.o.cmd").write_text("savedcmd_drivers/foo/foo.o := gcc -nostdinc -D__KERNEL__ -c -o drivers/foo/foo.o drivers/foo/foo.c\n")
    entry = be.compile_db().find(tree / "drivers" / "foo" / "foo.c")
    assert entry is not None and "-D__KERNEL__" in entry.clean_args()
    assert make(run_script="scripts/none.sh").configure([u]).returncode == 1


@pytest.mark.libclang
def test_semantic_check_against_aggregate(kern):
    tree, make, _ = kern
    be = make()
    u = _unit(tree, [("test_foo_clamp_ok", "KUNIT_EXPECT_EQ(test, foo_clamp(1, 0, 2), -EINVAL);"), ("test_foo_clamp_typo", "KUNIT_EXPECT_EQ(test, foo_clmp(1, 0, 2), 1);")])
    be.configure([u])
    chk = be.check(be.render(u))
    assert chk.available, chk.reason
    errors = [d for d in chk.diagnostics if d.severity == "error"]
    assert errors and {d.owner for d in errors} == {"test_foo_clamp_typo"}  # EINVAL은 <linux/errno.h>로 보인다


# ------------------------------------------------------------- 로그 픽스처
def test_ktap_fixture_keeps_only_our_suite():
    s = parse_ktap((LOGS / "ktap_mixed.log").read_text(encoding="utf-8"), {"foo_foo_clamp_test"})
    assert list(s) == ["foo_foo_clamp_test"]
    r = s["foo_foo_clamp_test"].results
    assert {k: v.status for k, v in r.items()} == {"test_foo_clamp_inside": "pass", "test_foo_clamp_low": "fail", "test_foo_clamp_loop": "timeout",
                                                   "test_foo_clamp_high": "skip"}
    assert r["test_foo_clamp_low"].observed == ["0"] and r["test_foo_clamp_low"].line == 12


def test_kernel_crash_fixtures():
    ctx = CrashContext(test_functions={"test_foo_clamp_crash", "test_foo_clamp_freed", "test_foo_clamp_range"}, target_functions={"foo_clamp"},
                       read_source_line=make_source_reader([TREE]))
    oops = build_brief((LOGS / "oops_null_deref.log").read_text(encoding="utf-8"), ctx, suspect_case="test_foo_clamp_crash")
    assert oops.category == "null_deref" and oops.location == "foo_clamp" and [c.split()[0] for c in oops.chain] == ["test_foo_clamp_crash", "foo_clamp"]
    kasan = build_brief((LOGS / "kasan_uaf.log").read_text(encoding="utf-8"), ctx)
    assert kasan.category == "kasan_uaf" and kasan.chain[-1].startswith("foo_clamp") and "kunit_kzalloc" in kasan.hypothesis
    bug = build_brief((LOGS / "bug_on.log").read_text(encoding="utf-8"), ctx)
    assert bug.category == "bug_on" and bug.location == "drivers/foo/foo.c:7" and bug.source_line == "return -EINVAL;"  # 마커가 가리킨 줄
    assert "전제 조건" in bug.hypothesis


def test_reduce_kunit_log_respects_budget():
    text = (LOGS / "oops_null_deref.log").read_text(encoding="utf-8")
    lines = strip_timestamps(text)
    idx = next(i for i, line in enumerate(lines) if line.startswith("BUG:"))
    full = reduce_kunit_log(lines, "# Subtest: foo_foo_clamp_test\nok test_foo_clamp_inside", "", "## 크래시 브리프\n분류: null_deref", idx, 4000)
    assert "### 크래시 직전 꼬리" in full and "foo: about to clamp" in full
    small = reduce_kunit_log(lines, "# Subtest: foo_foo_clamp_test\nok test_foo_clamp_inside", "x" * 400, "## 크래시 브리프\n" + "y" * 400, idx, 60)
    assert "### 우리 스위트 결과" in small and "foo: about to clamp" not in small  # 꼬리부터 줄인다


# --------------------------------------------------------------- 전 과정
@pytest.mark.libclang
def test_kunit_gen_end_to_end(kern, tmp_path):
    tree, _, history = kern
    home = tmp_path / "home"
    ensure_home_layout(home)
    add_product(home, "kern", tree, "kunit")
    ctx = make_context(home, "kern", overrides=["kunit.test_file=drivers/foo/foo_kunit.c", "kunit.test_dir=drivers/foo/tests", f"kunit.run_script={FAKE}",
                                                "coverage.enabled=true"])

    def case(n, b):
        return {"name": n, "purpose": n, "body": b}

    p = FakeProvider(script={
        "analyzer": [{"testable": True, "reason": "", "cautions": [], "recommended": []}],
        "test_writer": [
            {"cases": [case("test_foo_clamp_inside", "KUNIT_EXPECT_EQ(test, foo_clamp(5, 0, 10), 5); // COVERS=4,6,8,10,12")], "helpers": []},
            {"cases": [case("test_foo_clamp_low", "KUNIT_EXPECT_EQ(test, foo_clamp(-1, 0, 10), 1); // FAIL ACTUAL=0 COVERS=4,6,8,9"),
                       case("test_foo_clamp_crash", "KUNIT_EXPECT_EQ(test, foo_clamp(1, 2, 3), 2); // CRASH"),
                       case("test_foo_clamp_high", "KUNIT_EXPECT_EQ(test, foo_clamp(11, 0, 10), 10); // COVERS=4,6,8,10,11")], "helpers": []},
            {"cases": [case("test_foo_clamp_range", "KUNIT_EXPECT_EQ(test, foo_clamp(1, 5, 0), -EINVAL); // COVERS=4,6,7")], "helpers": []},
        ],
        "fixer": [{"strategy": "fix_case", "reason": "r", "body": "KUNIT_EXPECT_EQ(test, foo_clamp(-1, 0, 10), 0); // COVERS=4,6,8,9", "evidence": ":9",
                   "suspected_bug": False, "helpers": []},
                  {"strategy": "fix_case", "reason": "r", "body": "KUNIT_EXPECT_EQ(test, foo_clamp(2, 2, 3), 2); // COVERS=4,6,8,10,12", "evidence": "",
                   "suspected_bug": False, "helpers": []}],
    })
    orch = Orchestrator(ctx, tree / "drivers" / "foo" / "foo.c", GenOptions(functions=["foo_clamp"]), provider=p)
    st = orch.run()
    fs = st.functions["foo_clamp"]
    assert {c.name: c.status for c in fs.cases} == {n: CaseStatus.PASS for n in ("test_foo_clamp_inside", "test_foo_clamp_low", "test_foo_clamp_crash",
                                                                               "test_foo_clamp_high", "test_foo_clamp_range")}
    assert "not_run" in [h.status for h in fs.case("test_foo_clamp_high").history]
    assert fs.coverage.percent == 100.0 and fs.result == "complete" and fs.final_check == "ok"
    lf = fs.case("test_foo_clamp_low").last_failure  # Fixer가 받은 KUnit 단정 실패 요약
    assert lf.summary == "같지 않음: foo_clamp(-1, 0, 10) [0] ≠ 1" and (lf.expected, lf.actual) == ("1", "0")
    modes = _modes(history)
    assert modes[0] == "build" and set(modes[1:]) == {"all"}  # 사전검증만 build, 나머지는 빌드+실행 한 번
    installed = (tree / "drivers" / "foo" / "tests" / "foo_foo_clamp_test.c").read_text(encoding="utf-8")
    assert installed.count("KUNIT_CASE(") == 5 and "#include <linux/errno.h>" in installed
    assert "kunit_test_suites(&foo_foo_clamp_test);" in (tree / "drivers" / "foo" / "foo_kunit.c").read_text(encoding="utf-8")
    assert list(orch.rundir.path.rglob("foo_clamp.run.reduced.md"))
