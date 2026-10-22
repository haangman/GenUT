"""Reviewer 에이전트와 genut eval (설계 13.1 10단계)."""

import json

import pytest
from typer.testing import CliRunner

from conftest import SAMPLE, write_compile_db
from fake_backend import MarkerBackend
from genut.agents.base import AgentEnv
from genut.agents.reviewer import Reviewer
from genut.analysis.model import AnalysisReport, FunctionInfo
from genut.budget import Budget
from genut.cli import app
from genut.config import GenutConfig
from genut.context import make_context
from genut.evaluation import (
    EvalResult,
    EvalSummary,
    baseline_path,
    compare,
    function_metrics,
    load_corpus,
    load_result,
    render_markdown,
    run_eval,
    save_baseline,
    write_reports,
)
from genut.llm.fake_provider import FakeProvider
from genut.llm.tokens import TokenCounter
from genut.models import FailureInfo
from genut.paths import ensure_home_layout
from genut.products import add_product
from genut.rules import Rules
from genut.state import CaseEvent, CaseStatus, CoverageSummary, FunctionState, TestCase

RB = SAMPLE / "src" / "ring_buffer.c"


# --------------------------------------------------------------- Reviewer
def _fs():
    ok = TestCase(name="test_f_ok", target_function="f", purpose="정상", body="KUNIT_EXPECT_EQ(test, f(1), 1);", status=CaseStatus.PASS, origin="coverage",
                  covers_lines=[7, 8])
    fixed = TestCase(name="test_f_fixed", target_function="f", body="EXPECT_EQ(f(2), 0);", status=CaseStatus.PASS, previous_body="EXPECT_EQ(f(2), 2);", fixes_used=1,
                     fix_evidence=":5", last_failure=FailureInfo(category="assertion_failed", stage="run", summary="Which is: 0"))
    bad = TestCase(name="test_f_bad", target_function="f", body="x", status=CaseStatus.DROPPED)
    return FunctionState(name="f", cases=[ok, fixed, bad])


def test_reviewer_prompt_and_filtering():
    p = FakeProvider(script={"reviewer": [{"findings": [
        {"case": "test_f_fixed", "kind": "weakened_assertion", "detail": "기대값 2를 실제값 0으로 바꿈"},
        {"case": "test_f_fixed", "kind": "meaningless_assertion", "detail": "중복 지적"},
        {"case": "test_f_bad", "kind": "rule_violation", "detail": "제거된 케이스"},
    ]}]})
    cfg = GenutConfig()
    env = AgentEnv(p, TokenCounter(), cfg, Rules(common_text="## 반드시\n- 규칙"), budget=Budget(cfg.budget))
    fn = FunctionInfo(name="f", signature="int f(int)", return_type="int", file="f.c", start_line=1, end_line=3, source="int f(int x)\n{\n\treturn x;\n}")
    fs = _fs()
    res = Reviewer(env).review(AnalysisReport(function=fn), fs)
    assert [(f.case, f.kind) for f in res.findings] == [("test_f_fixed", "weakened_assertion")] and res.ignored == ["test_f_bad"]
    user = p.calls[0]["messages"][1].content
    assert "// test_f_ok [coverage, 미커버 줄 7,8을 겨냥]" in user and "test_f_bad" not in user
    assert "### test_f_fixed (수정 1회)\n고치기 전 실패: assertion_failed — Which is: 0\n고치기 전 본문:\nEXPECT_EQ(f(2), 2);\nFixer가 댄 근거: :5" in user
    assert "Review the passing cases of `f`" in user and fs.tokens_used > 0
    assert Reviewer(env).review(AnalysisReport(function=fn), FunctionState(name="f")).findings == []  # 통과 케이스가 없으면 부르지 않는다
    assert len(p.calls) == 1


# ------------------------------------------------------------------ eval
def test_load_corpus(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[[item]]\nfile = "src/ring_buffer.c"\nfunction = "rb_push"\n\n[[item]]\nfile = "src/str_util.c"\n', encoding="utf-8")
    assert [(i.file, i.function) for i in load_corpus(p)] == [("src/ring_buffer.c", "rb_push"), ("src/str_util.c", "")]
    p.write_text("title = 'x'\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_corpus(p)


def test_function_metrics_counts_build_and_categories():
    fs = FunctionState(name="f", result="partial", final_check="ok", tokens_used=900, coverage=CoverageSummary(covered=3, total=4, uncovered_lines=[9]))
    run = FailureInfo(category="assertion_failed", stage="run")
    comp = FailureInfo(category="undeclared_identifier", stage="compile")
    fs.cases = [
        TestCase(name="a", target_function="f", body="x", status=CaseStatus.PASS, history=[CaseEvent(round=1, stage="smoke", status="pass")]),
        TestCase(name="b", target_function="f", body="x", status=CaseStatus.DROPPED, last_failure=run, fixes_used=2, history=[CaseEvent(round=1, stage="expand", status="fail")]),
        TestCase(name="c", target_function="f", body="x", status=CaseStatus.DROPPED, last_failure=comp, history=[CaseEvent(round=1, stage="expand", status="fail")]),
        TestCase(name="d", target_function="f", body="x", status=CaseStatus.SUSPECTED_BUG, last_failure=run),
    ]
    m = function_metrics(fs)
    assert (m.written, m.built, m.passed, m.dropped, m.suspected_bugs, m.fixes, m.coverage) == (4, 3, 1, 2, 1, 2, 75.0)
    assert m.failure_categories == {"assertion_failed": 2, "undeclared_identifier": 1}


def _responder(tag, messages, schema):
    user = messages[-1].content
    if tag == "test_writer":
        fn = "rb_init" if "`rb_init`" in user else "rb_push"
        body = f"EXPECT_EQ({fn}(NULL, NULL, 0), RB_EINVAL);"
        if "Write exactly one" in user:
            return {"cases": [{"name": f"test_{fn}_null", "purpose": "NULL", "body": body}], "helpers": []}
        return {"cases": [{"name": f"test_{fn}_full", "purpose": "가득", "body": body + " // FAIL ACTUAL=0"}], "helpers": []}
    if tag == "fixer":
        return {"strategy": "drop_case", "reason": "포기", "body": "", "evidence": "", "suspected_bug": False, "helpers": []}
    return None


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    ensure_home_layout(home)
    add_product(home, "sample", SAMPLE, "gtest")
    db = write_compile_db(tmp_path / "cc.json")
    made = []

    def factory():
        be = MarkerBackend(db, tmp_path / "installed", [RB])
        made.append(be)
        return be

    return home, factory, made


@pytest.mark.libclang
def test_run_eval_measures_without_installing(world, tmp_path):
    home, factory, made = world
    ctx = make_context(home, "sample")
    items = load_corpus_from(tmp_path, [("src/ring_buffer.c", "rb_push"), ("src/ring_buffer.c", "rb_init"), ("src/missing.c", "x")])
    res, out = run_eval(ctx, items, corpus="c.toml", label="규칙 v1", provider=FakeProvider(responder=_responder), backend_factory=factory)
    s = res.summary
    assert (s.items, s.functions, s.errors) == (3, 2, 1) and s.model == "fake-model"
    assert s.salvaged_rate == 1.0 and s.complete_rate == 0.0 and s.case_pass_rate == 0.5 and s.case_build_rate == 1.0  # 실패로 제거된 케이스가 있어 partial
    assert s.failure_categories == {"assertion_failed": 2} and s.rules_hash and set(s.prompts) >= {"analyzer", "test_writer", "fixer", "reviewer"}
    assert all(not be.installed for be in made)  # 평가는 측정만 한다
    assert out.name.endswith("-v1") and json.loads((out / "results.json").read_text(encoding="utf-8"))["summary"]["functions"] == 2
    assert "src/missing.c" in render_markdown(res) and "오류: 파일이 없습니다" in render_markdown(res)
    runs = sorted((home / "products" / "sample" / "runs").iterdir())
    assert {r.name.split("-", 2)[2] for r in runs} == {"eval-ring_buffer-rb_push", "eval-ring_buffer-rb_init"}  # 같은 초면 이름 순서가 실행 순서와 다르다


def load_corpus_from(tmp_path, pairs):
    p = tmp_path / "c.toml"
    p.write_text("".join(f'[[item]]\nfile = "{f}"\nfunction = "{fn}"\n\n' for f, fn in pairs), encoding="utf-8")
    return load_corpus(p)


def test_baseline_compare_and_reports(tmp_path):
    base = EvalResult(label="기준", started="2026-10-01T10:00:00", summary=EvalSummary(model="m", complete_rate=0.5, case_pass_rate=0.6, avg_builds=5, avg_coverage=80.0))
    cur = EvalResult(label="새 규칙", started="2026-10-22T10:00:00", summary=EvalSummary(model="m", complete_rate=0.75, case_pass_rate=0.6, avg_builds=6, avg_coverage=90.0))
    rows = {t: (a, b, d) for t, a, b, d in compare(cur.summary, base.summary)}
    assert rows["complete 비율"] == ("75.0%", "50.0%", "+25.0%p 개선") and rows["케이스 통과율"][2] == ""
    assert rows["평균 빌드 횟수"][2] == "+1.0 악화" and rows["평균 라인 커버리지"] == ("90.0%", "80.0%", "+10.0%p 개선")
    path = save_baseline(tmp_path, base)
    assert path == baseline_path(tmp_path, "m") and load_result(path).summary.complete_rate == 0.5
    page = write_reports(cur, tmp_path, base)
    html = page.read_text(encoding="utf-8")
    assert "<td class='ok'>+25.0%p 개선</td>" in html and "<td class='bad'>+1.0 악화</td>" in html and "<script" not in html
    assert "| complete 비율 | 75.0% | 50.0% | +25.0%p 개선 |" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_cli_eval_needs_corpus(tmp_path):
    home, src = tmp_path / "h", tmp_path / "s"
    src.mkdir()
    add_product(home, "p", src)
    r = CliRunner().invoke(app, ["eval", "--home", str(home), "--product", "p"])
    assert r.exit_code == 2 and "코퍼스가 없습니다" in r.output


@pytest.mark.libclang
def test_sample_corpus_points_to_sample_project(tmp_path):
    from genut.analysis.compile_db import CompileDB
    from genut.analysis.extractor import analyze_file

    items = load_corpus(SAMPLE.parents[2] / "evals" / "sample_corpus.toml")
    db = CompileDB.load(write_compile_db(tmp_path / "cc.json"))
    for it in items:
        src = SAMPLE / it.file
        assert src.is_file(), it.file
        assert analyze_file(src, db.find(src)).get(it.function) is not None, it.function
    assert len(items) == 7
