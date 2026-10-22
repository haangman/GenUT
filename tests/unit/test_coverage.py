import json
import os
from pathlib import Path

import pytest

from conftest import FIXTURES, SAMPLE, write_compile_db
from genut.agents.analyzer import UnreachableAnalyzer
from genut.agents.base import AgentEnv
from genut.agents.test_writer import TestWriter
from genut.analysis.compile_db import CompileDB
from genut.analysis.extractor import analyze_file
from genut.analysis.linkscope import GTestLinkContext, judge_calls
from genut.analysis.model import AnalysisReport, FunctionInfo
from genut.backends.base import CommandResult, TestUnit
from genut.backends.gtest.backend import GTestBackend
from genut.budget import Budget
from genut.config import GenutConfig
from genut.coverage import FileCoverage, find_file, function_coverage, parse_gcov_json, parse_lcov, reach_conditions, render_uncovered, to_lcov
from genut.llm.fake_provider import FakeProvider
from genut.llm.tokens import TokenCounter
from genut.rules import Rules
from genut.state import FunctionState, TestCase

COV = FIXTURES / "coverage"
SRC = "/work/proj/src/ring_buffer.c"
RB_PUSH = FunctionInfo(name="rb_push", signature="int rb_push(struct rb *rb, const void *data, size_t len)", return_type="int", file=SRC, start_line=26, end_line=43)


def test_parse_lcov_and_function_range():
    files = parse_lcov((COV / "ring_buffer.partial.info").read_text(encoding="utf-8"))
    rb = find_file(files, SRC)
    assert rb is not None and rb.lines[26] == 3 and rb.lines[33] == 0 and len(files) == 2
    s = function_coverage(rb, RB_PUSH)
    assert (s.covered, s.total, s.uncovered_lines) == (10, 13, [33, 34, 38])  # 함수 밖 줄(9, 11)은 세지 않는다
    assert round(s.percent, 1) == 76.9
    full = function_coverage(find_file(parse_lcov((COV / "ring_buffer.full.info").read_text(encoding="utf-8")), SRC), RB_PUSH)
    assert full.percent == 100.0 and full.uncovered_lines == []
    assert function_coverage(None, RB_PUSH) is None


def test_parse_gcov_json_resolves_relative_paths():
    files = parse_gcov_json(json.loads((COV / "ring_buffer.gcov.json").read_text(encoding="utf-8")))
    rb = find_file(files, SRC)  # ../src/ring_buffer.c + 작업 디렉터리
    assert rb is not None and function_coverage(rb, RB_PUSH).uncovered_lines == [33, 34, 38]


def test_find_file_by_name_and_lcov_roundtrip():
    files = {"a": FileCoverage("/other/root/src/ring_buffer.c", {1: 1}), "b": FileCoverage("/x/slot.c", {2: 0})}
    assert find_file(files, "C:/build/src/ring_buffer.c").path.endswith("ring_buffer.c")  # 이름이 하나만 같으면 그것
    assert find_file({**files, "c": FileCoverage("/y/ring_buffer.c", {})}, "/z/ring_buffer.c") is None  # 둘 이상이면 모름
    back = parse_lcov(to_lcov(files))
    assert {Path(f.path).name: f.lines for f in back.values()} == {"ring_buffer.c": {1: 1}, "slot.c": {2: 0}}
    fc = FileCoverage("p", {1: 1, 2: 0})
    fc.merge(FileCoverage("p", {2: 3, 5: 0}))
    assert fc.lines == {1: 1, 2: 3, 5: 0}


@pytest.fixture
def rb_report(tmp_path):
    rb = SAMPLE / "src" / "ring_buffer.c"
    fn = analyze_file(rb, CompileDB.load(write_compile_db(tmp_path / "cc.json")).find(rb)).get("rb_push")
    return AnalysisReport(function=fn, linkscope=judge_calls(fn, rb, "gtest", Rules(), GTestLinkContext(product_root=SAMPLE, link_sources=[rb])))


@pytest.mark.libclang
def test_reach_conditions_for_sample(rb_report):
    items = reach_conditions(rb_report, [33, 34, 38])
    assert [i.lines for i in items] == [[33, 34], [38]]
    full, nomem = items
    assert (full.condition, full.branch_line, full.returns) == ("(rb_free_space(rb) < len) 참", 32, "RB_ENOSPC")
    assert full.related == ["rb_free_space (same_tu)", "파라미터 rb", "파라미터 len"]
    assert nomem.condition == "(slot == NULL) 참" and nomem.related == ["slot ← rb_alloc_slot()의 반환값, undefined → 스텁"]
    text = render_uncovered(items)
    assert text.splitlines()[0] == "미커버 :33-34  `stats.full_count++;`"
    assert "조건 (slot == NULL) 참 (:37) → RB_ENOMEM 반환" in text


def _env(provider, **budget):
    cfg = GenutConfig.model_validate({"budget": budget})
    return AgentEnv(provider, TokenCounter(), cfg, Rules(), budget=Budget(cfg.budget))


@pytest.mark.libclang
def test_test_writer_coverage_mode(rb_report):
    p = FakeProvider(script={"test_writer": [{"cases": [{"name": "test_rb_push_full", "purpose": "가득", "body": "EXPECT_EQ(rb_push(&rb, d, 9), RB_ENOSPC);"}], "helpers": []}]})
    fs = FunctionState(name="rb_push")
    text = render_uncovered(reach_conditions(rb_report, [33, 34]))
    out = TestWriter(_env(p)).write("coverage", rb_report, fs, uncovered=text, covers=[33, 34])
    assert out.cases[0].origin == "coverage" and out.cases[0].covers_lines == [33, 34]
    user = p.calls[0]["messages"][1].content
    assert "## 미커버 줄과 도달 조건\n\n미커버 :33-34" in user and "execute the lines listed" in user
    with pytest.raises(ValueError):
        TestWriter(_env(p)).write("coverage", rb_report, fs)


@pytest.mark.libclang
def test_unreachable_analyzer_defaults_to_unknown(rb_report):
    p = FakeProvider(script={"unreachable": [{"lines": [{"line": 38, "category": "needs_stub_behavior", "reason": "rb_alloc_slot이 NULL을 돌려줘야 함"},
                                                        {"line": 99, "category": "optimized_out", "reason": "목록에 없는 줄"}]}]})
    fs = FunctionState(name="rb_push")
    got = UnreachableAnalyzer(_env(p)).classify(rb_report, fs, "미커버 ...", [33, 38])
    assert [(u.line, u.category) for u in got] == [(33, "unknown"), (38, "needs_stub_behavior")]
    assert "Classify every uncovered line of `rb_push`" in p.calls[0]["messages"][1].content and fs.tokens_used > 0


# --------------------------------------------------------- gtest 측정
class CovRunner:
    def __init__(self, build: Path, output: str = "", lcov: str = ""):
        self.build, self.output, self.lcov, self.calls = build, output, lcov, []
        self.gcda: Path | None = None  # 실행 파일이 실행 중에 다시 쓰는 카운터 파일

    def __call__(self, cmd, cwd=None, timeout=None, env=None):
        self.calls.append((list(cmd), cwd, env))
        name = Path(cmd[0]).name
        if name.startswith("gcov"):
            return CommandResult(cmd, 0, self.output + "\ngcov: warning: 경고는 stderr로\n", 0.1)
        if name.startswith("llvm-cov"):
            return CommandResult(cmd, 0, self.lcov, 0.1)
        if name.startswith("llvm-profdata"):
            return CommandResult(cmd, 0, "", 0.1)
        if self.gcda is not None:
            self.stale_at_run = self.gcda.exists()
            self.gcda.write_text("new counters")
        if env and "LLVM_PROFILE_FILE" in env:  # 실행 파일이 profraw를 남긴다
            Path(env["LLVM_PROFILE_FILE"].replace("%p", "77")).write_text("raw")
        return CommandResult(cmd, 0, "[  PASSED  ]", 0.1)


def _gtest(tmp_path, tool, runner_kw):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    src = root / "src" / "ring_buffer.c"
    src.write_text("int x;\n")
    cfg = GenutConfig.model_validate({"coverage": {"tool": tool}})
    runner = CovRunner(root / "build-genut", **runner_kw)
    be = GTestBackend(cfg, root, runner=runner)
    obj = be.build_dir / "CMakeFiles" / "rb.dir" / "src"
    obj.mkdir(parents=True)
    (obj / "ring_buffer.c.gcda").write_text("old counters")
    runner.gcda = obj / "ring_buffer.c.gcda" if tool == "gcov" else None
    exe = be.build_dir / ("genut_ring_buffer_rb_push" + (".exe" if os.name == "nt" else ""))
    exe.write_text("exe")
    unit = TestUnit(source=src, report=AnalysisReport(function=RB_PUSH.model_copy(update={"file": str(src)})), state=FunctionState(name="rb_push",
                    cases=[TestCase(name="test_ok", target_function="rb_push", body="x;")]))
    return be, runner, be.render(unit), obj


def test_gtest_coverage_with_gcov(tmp_path):
    data = json.loads((COV / "ring_buffer.gcov.json").read_text(encoding="utf-8"))
    data["current_working_directory"] = str(tmp_path / "proj" / "build-genut")
    be, runner, r, obj = _gtest(tmp_path, "gcov", {"output": json.dumps(data)})
    fc = be.coverage(r, ["test_ok"], tmp_path / "out")
    run_cmd, run_cwd, _ = runner.calls[0]
    assert run_cmd[-1] == "--gtest_filter=RingBufferRbPushTest.test_ok" and Path(run_cwd).name == "cov-rb_push"
    assert runner.stale_at_run is False  # 이전 카운터는 지우고 잰다
    gcov_cmd, gcov_cwd, _ = runner.calls[1]
    assert gcov_cmd[1:] == ["--json-format", "--stdout", "ring_buffer.c.gcda"] and Path(gcov_cwd) == obj
    assert fc is not None and function_coverage(fc, RB_PUSH).uncovered_lines == [33, 34, 38]
    assert "DA:33,0" in (tmp_path / "out" / "rb_push.coverage.info").read_text(encoding="utf-8")
    assert be.coverage(r, [], tmp_path / "out") is None  # 통과 케이스가 없으면 재지 않는다


def test_gtest_coverage_with_llvm_cov(tmp_path):
    lcov = (COV / "ring_buffer.full.info").read_text(encoding="utf-8").replace(SRC, str(tmp_path / "proj" / "src" / "ring_buffer.c"))
    be, runner, r, _ = _gtest(tmp_path, "llvm-cov", {"lcov": lcov})
    fc = be.coverage(r, ["test_ok"], tmp_path / "out")
    _, _, env = runner.calls[0]
    assert env["LLVM_PROFILE_FILE"].endswith("cov-%p.profraw")
    merge, export = runner.calls[1][0], runner.calls[2][0]
    assert merge[1:3] == ["merge", "-sparse"] and merge[-2] == "-o"
    assert export[1:3] == ["export", "-format=lcov"] and export[-1] == str(be._exe("genut_ring_buffer_rb_push"))
    assert function_coverage(fc, RB_PUSH).percent == 100.0


def test_sibling_tool_matches_compiler(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for n in ("x86_64-linux-gnu-gcc-13", "x86_64-linux-gnu-gcov-13", "cc.exe", "gcov.exe"):
        (bindir / n).write_text("")
    be, *_ = _gtest(tmp_path, "gcov", {})
    db = be.build_dir / "compile_commands.json"
    for compiler, expected in ((bindir / "x86_64-linux-gnu-gcc-13", "x86_64-linux-gnu-gcov-13"), (bindir / "cc.exe", "gcov.exe")):
        db.write_text(json.dumps([{"directory": str(be.root), "arguments": [str(compiler), "-c", "src/ring_buffer.c"], "file": "src/ring_buffer.c"}]))
        assert Path(be._sibling_tool(be.root / "src" / "ring_buffer.c", "gcov")).name == expected
