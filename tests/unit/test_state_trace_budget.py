import json
from datetime import datetime

from genut.budget import Budget
from genut.config import BudgetConfig
from genut.report import classify_result, render_report, render_summary
from genut.state import CaseStatus, CoverageSummary, FunctionState, RunState, SharedArea, TestCase
from genut.trace import RunDir, Trace, resolve_run


def _fn():
    fn = FunctionState(name="rb_push")
    fn.cases = [
        TestCase(name="test_a", target_function="rb_push", body="x;", status=CaseStatus.PASS),
        TestCase(name="test_b", target_function="rb_push", body="y;", status=CaseStatus.DROPPED, drop_reason="수정 한도"),
    ]
    return fn


def test_result_classification():
    fn = _fn()
    assert classify_result(fn) == "partial"
    fn.cases[1].status = CaseStatus.SUSPECTED_BUG
    assert classify_result(fn) == "complete"
    fn.coverage = CoverageSummary(covered=8, total=10)
    assert classify_result(fn) == "partial"
    for c in fn.cases:
        c.status = CaseStatus.DROPPED
    assert classify_result(fn) == "none"


def test_shared_area_rollback():
    fn = FunctionState(name="f")
    fn.change_shared(SharedArea(includes=['#include "a.h"']))
    assert fn.shared.version == 1
    fn.change_shared(SharedArea(includes=['#include "a.h"', '#include "b.h"']))
    assert fn.shared.version == 2
    assert fn.rollback_shared()
    assert fn.shared.version == 1 and fn.shared.includes == ['#include "a.h"']


def test_unique_name():
    fn = _fn()
    assert fn.unique_name("test_c") == "test_c"
    assert fn.unique_name("test_a") == "test_a_2"


def test_state_save_load(tmp_path):
    st = RunState(run_id="r1", source_file="src/a.c", backend="gtest")
    st.functions["rb_push"] = _fn()
    p = tmp_path / "state.json"
    st.save(p)
    back = RunState.load(p)
    assert back.functions["rb_push"].cases[0].status == CaseStatus.PASS
    assert not p.with_suffix(".tmp").exists()


def test_budget_reserve_and_tokens():
    t = [0.0]
    b = Budget(BudgetConfig(max_builds_per_run=3, final_build_reserve=1, max_seconds_per_run=10, max_tokens_per_function=100), clock=lambda: t[0])
    assert b.builds_left() == 2
    b.record_build()
    b.record_build()
    assert not b.can_build()
    assert b.can_build(use_reserve=True)
    b.add_tokens("f", 60)
    assert not b.tokens_exceeded("f")
    b.add_tokens("f", 50)
    assert b.tokens_exceeded("f")
    t[0] = 11.0
    assert b.time_exceeded()
    assert not b.can_build()
    assert b.can_build(use_reserve=True)  # 최종 확인은 시간 초과여도 한다


def test_rundir_and_trace(tmp_path):
    runs = tmp_path / "runs"
    rd = RunDir.create(runs, "rb push", now=datetime(2026, 10, 22, 14, 30, 12))
    assert rd.path.name == "20261022-143012-rb_push"
    rd2 = RunDir.create(runs, "rb push", now=datetime(2026, 10, 22, 14, 30, 12))
    assert rd2.path.name.endswith("-2")
    seen = []
    tr = Trace(rd, echo=seen.append)
    with tr.stage("analysis"):
        tr.say("[1/8] 분석")
    p = tr.prompt("analyzer", "sys", "user", '{"ok":true}', {"ok": True}, {"tokens": 10})
    tr.close()
    events = [json.loads(line) for line in rd.trace_jsonl.read_text(encoding="utf-8").splitlines()]
    assert [e["type"] for e in events] == ["stage_start", "message", "stage_end", "llm_call"]
    assert seen == ["[1/8] 분석"]
    assert p.name == "01-analyzer.md" and "## response" in p.read_text(encoding="utf-8")
    assert resolve_run(runs, "latest") == rd2.path
    assert resolve_run(runs, rd.run_id) == rd.path


def test_report_and_summary(tmp_path):
    st = RunState(run_id="r1", source_file="src/a.c", backend="kunit")
    fn = _fn()
    fn.result = classify_result(fn)
    fn.final_check = "ok"
    st.functions["rb_push"] = fn
    md = render_report(st, {"builds_used": 3, "builds_max": 10, "elapsed_sec": 5, "tokens": {"rb_push": 10}})
    assert "partial" in md and "`test_a`" in md and "수정 한도" in md
    st.save(tmp_path / "state.json")
    out = render_summary(tmp_path)
    assert "rb_push: partial" in out and "최종 확인 OK" in out


def test_say_survives_console_encoding_errors(tmp_path):
    shown = []

    def cp949_console(text):
        text.encode("cp949")  # ✓ 같은 문자는 cp949로 못 쓴다
        shown.append(text)

    tr = Trace(RunDir(tmp_path / "run"), echo=cp949_console)
    tr.say("round 1: fix_case(test_a) ✓")
    tr.close()
    assert shown == ["round 1: fix_case(test_a) ?"]
    assert "✓" in tr.rundir.trace_jsonl.read_text(encoding="utf-8")  # 기록에는 원문이 남는다
