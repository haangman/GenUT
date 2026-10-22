"""오케스트레이터 시나리오 (설계 13.1 7단계 검증): FakeProvider + 표지로 결과를 정하는 가짜 백엔드."""

import json
from types import SimpleNamespace

import pytest

from conftest import SAMPLE, write_compile_db
from fake_backend import MarkerBackend
from genut import orchestrator
from genut.context import make_context
from genut.llm.fake_provider import FakeProvider
from genut.orchestrator import GenOptions, Orchestrator, finalize_run
from genut.paths import ensure_home_layout
from genut.products import add_product
from genut.replay import ReplayProvider, replay_run
from genut.state import CaseStatus, FunctionStatus
from genut.viewer.render import render_run, write_view

pytestmark = pytest.mark.libclang

RB = SAMPLE / "src" / "ring_buffer.c"
OK = 'struct rb rb;\nunsigned char buf[8];\nrb_init(&rb, buf, 8);\nEXPECT_EQ(rb_push(&rb, "a", 1), RB_OK);'
NULL = 'EXPECT_EQ(rb_push(NULL, "a", 1), RB_EINVAL);'
FULL_BAD = 'EXPECT_EQ(rb_push(&rb, "abcdef", 6), RB_OK); // FAIL ACTUAL=-28'
FULL_OK = 'EXPECT_EQ(rb_push(&rb, "abcdef", 6), RB_ENOSPC);'
ANALYZED = {"testable": True, "reason": "", "cautions": ["rb를 먼저 초기화"], "recommended": []}


def cases(*items, helpers=()):
    return {"cases": [{"name": n, "purpose": f"{n} 목적", "body": b} for n, b in items], "helpers": [{"name": n, "code": c} for n, c in helpers]}


def fix(strategy="fix_case", body="", evidence="", suspected=False, helpers=(), reason="이유"):
    return {"strategy": strategy, "reason": reason, "body": body, "evidence": evidence, "suspected_bug": suspected,
            "helpers": [{"name": n, "code": c} for n, c in helpers]}


SMOKE = cases(("test_rb_push_ok", OK))


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    ensure_home_layout(home)
    add_product(home, "sample", SAMPLE, "gtest")
    be = MarkerBackend(write_compile_db(tmp_path / "cc.json"), tmp_path / "installed", [RB, SAMPLE / "src" / "slot.c"])
    return SimpleNamespace(home=home, be=be, tmp=tmp_path, kdir=home / "products" / "sample" / "knowledge")


def run(world, script=None, overrides=(), opts=None, provider=None):
    ctx = make_context(world.home, "sample", overrides=list(overrides))
    p = provider or FakeProvider(script={"analyzer": [ANALYZED], **(script or {})})
    orch = Orchestrator(ctx, RB, opts or GenOptions(functions=["rb_push"]), provider=p, backend=world.be)
    st = orch.run()
    return st, orch, p


def statuses(st, fn="rb_push"):
    return {c.name: c.status for c in st.functions[fn].cases}


def events(orch, type_):
    lines = orch.rundir.trace_jsonl.read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["type"] == type_]


def fixer_prompts(p):
    return [c["messages"][1].content for c in p.calls if c["tag"] == "fixer"]


# ------------------------------------------------------------- 기본 흐름
def test_fail_once_then_fixed(world):
    st, orch, p = run(world, {"test_writer": [SMOKE, cases(("test_rb_push_full", FULL_BAD), ("test_rb_push_null", NULL))],
                              "fixer": [fix(body=FULL_OK, evidence=":34 RB_ENOSPC")]})
    fs = st.functions["rb_push"]
    assert statuses(st) == {"test_rb_push_ok": CaseStatus.PASS, "test_rb_push_full": CaseStatus.PASS, "test_rb_push_null": CaseStatus.PASS}
    assert fs.case("test_rb_push_full").fixes_used == 1 and fs.result == "complete" and fs.final_check == "ok"
    assert len(world.be.builds) == 5  # 사전검증 + 스모크 + 확장 + 수정 + 최종 확인
    assert world.be.installed["rb_push"].count("TEST_F(") == 3 and fs.installed_path.endswith("ring_buffer_rb_push_test.cc")
    run_dir = orch.rundir.path
    assert (run_dir / "final" / "report.md").exists() and (run_dir / "fixes" / "r1-test_rb_push_full.md").exists()
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["result"] == {"rb_push": "complete"} and meta["budget"]["builds_used"] == 5
    assert "RB_ENOSPC" in (world.kdir / "fix_patterns.yaml").read_text(encoding="utf-8")  # 성공한 수정은 다음 실행의 예시
    fx = events(orch, "fix")[0]
    assert (fx["category"], fx["recommended"], fx["chosen"]) == ("assertion_failed", "fix_case", "fix_case")
    assert "실제값: -28" in fixer_prompts(p)[0] and "통과 케이스 (손대지 말 것)" in fixer_prompts(p)[0]


def test_drop_case_records_suspected_bug(world):
    st, *_ = run(world, {"test_writer": [SMOKE, cases(("test_rb_push_full", FULL_BAD))],
                         "fixer": [fix("drop_case", suspected=True, reason="소스가 공간 검사를 잘못함")]})
    assert statuses(st)["test_rb_push_full"] == CaseStatus.SUSPECTED_BUG
    assert st.functions["rb_push"].result == "complete"  # 소스 버그 의심은 실패로 치지 않는다
    assert "test_rb_push_full" in (world.kdir / "suspected_bugs.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("evidence,expected", [("", CaseStatus.SUSPECTED_BUG), (":34 반환 지점 RB_ENOSPC", CaseStatus.PASS)])
def test_copying_actual_value_needs_evidence(world, evidence, expected):
    copied = 'EXPECT_EQ(rb_push(&rb, "abcdef", 6), -28);'
    st, *_ = run(world, {"test_writer": [SMOKE, cases(("test_rb_push_full", FULL_BAD))], "fixer": [fix(body=copied, evidence=evidence)]})
    c = st.functions["rb_push"].case("test_rb_push_full")
    assert c.status == expected
    if expected == CaseStatus.SUSPECTED_BUG:
        assert "실제값" in c.drop_reason


def test_give_up_keeps_passing_cases(world):
    st, *_ = run(world, {"test_writer": [SMOKE, cases(("test_rb_push_full", FULL_BAD), ("test_rb_push_huge", FULL_BAD))],
                         "fixer": [fix("give_up", reason="더 시도할 가치가 없음")]})
    fs = st.functions["rb_push"]
    assert fs.status == FunctionStatus.GIVEN_UP and fs.case("test_rb_push_ok").status == CaseStatus.PASS
    assert all(fs.case(n).drop_reason.startswith("give_up") for n in ("test_rb_push_full", "test_rb_push_huge"))
    assert fs.final_check == "ok" and world.be.installed["rb_push"].count("TEST_F(") == 1 and fs.result == "partial"


def test_shared_regression_rolls_back(world):
    make = "static void make_rb(struct rb *r) { }"
    ok_helper = OK + "\nmake_rb(&rb); // USES_HELPER"
    st, orch, p = run(world, {
        "test_writer": [cases(("test_rb_push_ok", ok_helper), helpers=[("make_rb", make)]), cases(("test_rb_push_full", FULL_BAD))],
        "fixer": [fix(body=FULL_OK, evidence=":34", helpers=[("make_rb", "static void make_rb(struct rb *r) { /* BREAKS_OTHERS */ }")]),
                  fix(body=FULL_OK + " // 헬퍼 없이", evidence=":34")],
    })
    fs = st.functions["rb_push"]
    assert fs.shared.helpers["make_rb"] == make  # 깨뜨린 변경은 되돌렸다
    assert statuses(st) == {"test_rb_push_ok": CaseStatus.PASS, "test_rb_push_full": CaseStatus.PASS}
    rb = events(orch, "shared_rollback")
    assert rb and rb[0]["owner"] == "test_rb_push_full" and rb[0]["regressed"] == ["test_rb_push_ok"]
    assert "must not change shared helpers" in fixer_prompts(p)[1]
    assert "BREAKS_OTHERS" not in world.be.installed["rb_push"]


def test_build_budget_exhausted_keeps_passes(world):
    st, *_ = run(world, {"test_writer": [SMOKE, cases(("test_rb_push_full", FULL_BAD), ("test_rb_push_null", NULL))]},
                 overrides=["budget.max_builds_per_run=4", "budget.final_build_reserve=1"])
    fs = st.functions["rb_push"]
    assert "빌드 예산" in fs.case("test_rb_push_full").drop_reason
    assert fs.final_check == "ok" and world.be.installed["rb_push"].count("TEST_F(") == 2 and len(world.be.builds) == 4


def test_interrupt_falls_back_to_green_snapshot(world):
    calls = {"n": 0}

    def responder(tag, messages, schema):
        if tag == "test_writer":
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt
        return None

    p = FakeProvider(responder=responder, script={"analyzer": [ANALYZED], "test_writer": [SMOKE]})
    st, orch, _ = run(world, provider=p)
    fs = st.functions["rb_push"]
    assert st.interrupted and fs.final_check == "snapshot" and len(world.be.builds) == 2  # 최종 확인 빌드는 하지 않았다
    assert world.be.installed["rb_push"].count("TEST_F(") == 1 and st.snapshots[0].cases == ["test_rb_push_ok"]


def test_final_build_failure_uses_largest_snapshot(world):
    world.be.fail_builds_from = 5
    st, *_ = run(world, {"test_writer": [SMOKE, cases(("test_rb_push_full", FULL_BAD), ("test_rb_push_null", NULL))],
                         "fixer": [fix(body=FULL_OK, evidence=":34")]})
    fs = st.functions["rb_push"]
    assert fs.final_check == "snapshot" and world.be.installed["rb_push"].count("TEST_F(") == 3
    assert max(len(s.cases) for s in st.snapshots) == 3


def test_case_failing_only_in_final_check_is_dropped(world):
    st, *_ = run(world, {"test_writer": [SMOKE, cases(("test_rb_push_null", NULL + " // FAILS_IN_FINAL"))]})
    fs = st.functions["rb_push"]
    assert fs.case("test_rb_push_null").status == CaseStatus.FLAKY
    assert fs.final_check == "partial" and world.be.installed["rb_push"].count("TEST_F(") == 1


# ---------------------------------------------------------- 실패 종류별
def test_crash_isolation_and_not_run(world):
    st, orch, _ = run(world, {"test_writer": [SMOKE, cases(("test_rb_push_bad", OK + " // CRASH"), ("test_rb_push_null", NULL))],
                              "fixer": [fix(body=OK + " // 고침")]})
    fs = st.functions["rb_push"]
    assert statuses(st) == {"test_rb_push_ok": CaseStatus.PASS, "test_rb_push_bad": CaseStatus.PASS, "test_rb_push_null": CaseStatus.PASS}
    bad = fs.case("test_rb_push_bad")
    assert [h.status for h in bad.history][:2] == ["new", "crash"] and "격리 실행에서 재현" in bad.last_failure.details
    null_hist = [h.status for h in fs.case("test_rb_push_null").history]
    assert "not_run" in null_hist and null_hist[-1] == "pass"
    assert ("rb_push", ["test_rb_push_bad"]) in world.be.runs  # 용의자만 다시 실행
    assert list((orch.rundir.path / "crash").glob("*.md"))


def test_timeout_gets_one_fix_attempt(world):
    st, _, p = run(world, {"test_writer": [SMOKE, cases(("test_rb_push_loop", OK + " // TIMEOUT"))], "fixer": [fix(body=NULL + " // 고침")]})
    assert statuses(st)["test_rb_push_loop"] == CaseStatus.PASS
    assert "Allowed strategies (prefer the first): fix_case" in fixer_prompts(p)[0]


def test_add_stub_without_stub_tool_narrows_scope(world):
    st, *_ = run(world, {"test_writer": [SMOKE, cases(("test_rb_push_slot", OK + " // LINK_ERROR"))], "fixer": [fix("add_stub", body=OK)]})
    fs = st.functions["rb_push"]
    c = fs.case("test_rb_push_slot")
    assert c.status == CaseStatus.DROPPED and "스텁 도구 연동 필요" in c.drop_reason
    assert "undefined reference to `rb_alloc_slot`" in c.history[1].note  # 귀속할 수 없는 링크 오류도 그 케이스의 실패로
    assert any("스텁 도구 연동 필요" in n for n in fs.narrowed) and fs.case("test_rb_push_ok").status == CaseStatus.PASS


def test_semantic_check_error_is_caught_before_build(world):
    st, *_ = run(world, {"test_writer": [SMOKE, cases(("test_rb_push_typo", NULL + " // CHECK_ERROR"))], "fixer": [fix(body=NULL)]})
    c = st.functions["rb_push"].case("test_rb_push_typo")
    assert c.status == CaseStatus.PASS and c.history[1].stage == "expand" and c.history[1].status == "fail"
    assert "test_rb_push_typo" not in ",".join(world.be.builds[2])  # 의미 검사에 걸린 케이스는 그 라운드 빌드에서 빠진다


def test_review_reject_goes_back_to_fixer(world):
    st, *_ = run(world, {"test_writer": [SMOKE, cases(("test_rb_push_noassert", 'rb_push(NULL, "a", 1);'))], "fixer": [fix(body=NULL)]})
    c = st.functions["rb_push"].case("test_rb_push_noassert")
    assert c.status == CaseStatus.PASS and c.history[1].status == "fail" and c.fixes_used == 1


def test_flaky_case_is_dropped_after_second_failure(world):
    st, *_ = run(world, {"test_writer": [cases(("test_rb_push_ok", OK + " // FLAKY")), cases(("test_rb_push_full", FULL_BAD))],
                         "fixer": [fix(body=FULL_OK, evidence=":34")]})
    c = st.functions["rb_push"].case("test_rb_push_ok")
    assert c.status == CaseStatus.FLAKY and "불안정" in c.drop_reason


def test_candidates_are_merged_by_case(world):
    st, *_ = run(world, {"test_writer": [SMOKE, SMOKE, cases(("test_rb_push_null", NULL)), cases()]}, overrides=["budget.candidates=2"])
    fs = st.functions["rb_push"]
    assert fs.case("test_rb_push_ok_2").status == CaseStatus.DROPPED and "후보 합치기" in fs.case("test_rb_push_ok_2").drop_reason
    assert fs.case("test_rb_push_null").status == CaseStatus.PASS


def test_slow_preflight_skips_smoke(world):
    world.be.preflight_seconds = 500
    st, orch, p = run(world, {"test_writer": [cases(("test_rb_push_null", NULL))]})
    assert st.smoke_skipped and statuses(st) == {"test_rb_push_null": CaseStatus.PASS}
    assert [c["tag"] for c in p.calls] == ["analyzer", "test_writer", "reviewer"]
    assert not list((orch.rundir.path / "prompts").glob("*smoke*"))


# ------------------------------------------------------------- 멈춤·관찰
def test_static_function_is_untestable_without_stub_tool(world):
    st, _, p = run(world, opts=GenOptions(functions=["rb_free_space"]))
    fs = st.functions["rb_free_space"]
    assert fs.status == FunctionStatus.UNTESTABLE and "스텁 도구 연동 필요" in fs.status_reason
    assert world.be.builds == [] and fs.result == "none" and [c["tag"] for c in p.calls] == ["analyzer"]


def test_unknown_function_and_preflight_failure_stop(world):
    _, orch, _ = run(world, opts=GenOptions(functions=["nope"]))
    assert orch.error == "input_error"
    world.be.preflight_ok = False
    st, orch, p = run(world)
    assert orch.error == "infra_error" and [c["tag"] for c in p.calls] == ["analyzer"] and "뼈대 빌드 실패" in st.notes[-1]


def test_dry_run_records_prompts_only(world):
    ctx = make_context(world.home, "sample")
    orch = Orchestrator(ctx, RB, GenOptions(functions=["rb_push"], dry_run=True), backend=world.be)
    orch.run()
    names = sorted(p.name for p in (orch.rundir.path / "prompts").iterdir())
    assert names == ["01-analyzer.md", "02-test_writer-smoke.md", "03-test_writer-expand.md"]
    assert world.be.builds == [] and world.be.installed == {}
    assert json.loads((orch.rundir.path / "run.json").read_text(encoding="utf-8"))["dry_run"] is True


def test_stop_after_then_finalize(world):
    st, orch, _ = run(world, {"test_writer": [SMOKE]}, opts=GenOptions(functions=["rb_push"], stop_after="smoke"))
    assert st.stage == "smoke" and not st.finished and world.be.installed == {}
    ctx = make_context(world.home, "sample")
    done = finalize_run(ctx, orch.rundir.path, backend=world.be)
    fs = done.functions["rb_push"]
    assert done.finished and fs.final_check == "ok" and world.be.installed["rb_push"].count("TEST_F(") == 1
    assert done.builds_used == st.builds_used + 1


def test_finalize_uses_the_runs_recorded_config(world, monkeypatch):
    _, orch, _ = run(world, {"test_writer": [SMOKE]}, overrides=["gtest.link_targets=[rb,rbslot]"], opts=GenOptions(functions=["rb_push"], stop_after="smoke"))
    seen = {}

    def fake_make_backend(ctx):
        seen["links"] = ctx.config.gtest.link_targets
        return world.be

    monkeypatch.setattr(orchestrator, "make_backend", fake_make_backend)
    finalize_run(make_context(world.home, "sample"), orch.rundir.path)  # 지금 설정에는 link_targets가 없다
    assert seen["links"] == ["rb", "rbslot"]


def test_replay_reuses_recorded_responses(world):
    script = {"test_writer": [SMOKE, cases(("test_rb_push_full", FULL_BAD))], "fixer": [fix(body=FULL_OK, evidence=":34")]}
    first, orch, p = run(world, script)
    ctx = make_context(world.home, "sample")
    world.be.installed.clear()
    again = replay_run(ctx, orch.rundir.path, backend=world.be)
    assert isinstance(again.provider, ReplayProvider) and again.provider.served == len(p.calls)
    assert statuses(again.state) == statuses(first) and again.state.functions["rb_push"].tokens_used == 0
    assert json.loads((again.rundir.path / "run.json").read_text(encoding="utf-8"))["replay_of"] == orch.rundir.run_id

    live = FakeProvider(script={"fixer": [fix("drop_case", reason="다시 받은 응답")]})
    third = replay_run(ctx, orch.rundir.path, live_from="fix", live=live, backend=world.be)
    assert [c["tag"] for c in live.calls] == ["fixer", "reviewer"]  # 수정 단계와 그 뒤 단계(리뷰)만 실제 provider
    assert third.state.functions["rb_push"].case("test_rb_push_full").drop_reason == "다시 받은 응답"


def test_viewer_renders_run(world):
    _, orch, _ = run(world, {"test_writer": [SMOKE, cases(("test_rb_push_full", FULL_BAD))], "fixer": [fix(body=FULL_OK, evidence=":34")]})
    page = render_run(orch.rundir.path)
    assert page.startswith("<!doctype html>") and "케이스 보드" in page and "타임라인" in page
    assert "test_rb_push_full" in page and "rb_push(&amp;rb, &quot;abcdef&quot;, 6)" in page and "<script" not in page
    assert "prompts/01-analyzer.md" in page and "수정 기록" in page
    assert write_view(orch.rundir.path).name == "view.html"


# ------------------------------------------------------------- ⑦ 커버리지
LINES = [26, 30, 31, 32, 33, 34, 36, 37, 38, 39, 40, 41, 42]
COV_SMOKE = cases(("test_rb_push_ok", OK + " // COVERS=30,32,36,37,39,40,41,42"))
COV_EXPAND = cases(("test_rb_push_null", NULL + " // COVERS=30,31"))


def test_coverage_round_reaches_target(world):
    world.be.executable_lines, world.be.entry_lines = LINES, {26}
    more = cases(("test_rb_push_full", FULL_OK + " // COVERS=32,33,34"), ("test_rb_push_nomem", OK + " // COVERS=36,37,38"))
    st, orch, p = run(world, {"test_writer": [COV_SMOKE, COV_EXPAND, more]})
    fs = st.functions["rb_push"]
    assert fs.coverage.percent == 100.0 and fs.result == "complete" and fs.unreachable == []
    full = fs.case("test_rb_push_full")
    assert full.origin == "coverage" and full.covers_lines == [33, 34, 38] and full.status == CaseStatus.PASS
    prompt = [c for c in p.calls if c["tag"] == "test_writer"][-1]["messages"][1].content
    assert "미커버 :33-34" in prompt and "(rb_free_space(rb) < len) 참" in prompt and "(slot == NULL) 참" in prompt
    assert world.be.coverage_calls[0] == ["test_rb_push_ok", "test_rb_push_null"]  # 통과 케이스만으로 잰다
    assert len(world.be.builds) == 5  # 사전검증 + 스모크 + 확장 + 커버리지 라운드 + 최종 확인
    assert [e["uncovered"] for e in events(orch, "coverage")][:2] == [[33, 34, 38], []]


def test_remaining_uncovered_lines_are_classified(world):
    world.be.executable_lines, world.be.entry_lines = LINES, {26}
    st, orch, p = run(world, {
        "test_writer": [COV_SMOKE, COV_EXPAND, cases(("test_rb_push_full", FULL_OK + " // COVERS=32,33,34"))],
        "unreachable": [{"lines": [{"line": 38, "category": "needs_stub_behavior", "reason": "rb_alloc_slot이 NULL을 돌려줘야 한다"}]}],
    }, overrides=["budget.max_coverage_rounds=1"])
    fs = st.functions["rb_push"]
    assert fs.coverage.uncovered_lines == [38] and fs.result == "partial"
    assert [(u.line, u.category) for u in fs.unreachable] == [(38, "needs_stub_behavior")]
    assert ":38 needs_stub_behavior" in (orch.rundir.path / "final" / "report.md").read_text(encoding="utf-8")
    assert [c["tag"] for c in p.calls].count("test_writer") == 3


def test_coverage_disabled_skips_measurement(world):
    world.be.executable_lines = LINES
    st, *_ = run(world, {"test_writer": [SMOKE]}, overrides=["coverage.enabled=false"])
    assert world.be.coverage_calls == [] and st.functions["rb_push"].coverage is None


# ---------------------------------------------------------------- ⑧ 리뷰
def review(*findings):
    return {"findings": [{"case": c, "kind": k, "detail": d} for c, k, d in findings]}


def test_reviewer_finding_gets_one_fix(world):
    st, orch, p = run(world, {
        "test_writer": [SMOKE, cases(("test_rb_push_null", NULL))],
        "reviewer": [review(("test_rb_push_null", "meaningless_assertion", "NULL만 확인해 반환값의 의미를 보지 않는다"))],
        "fixer": [fix(body=NULL + "\nEXPECT_EQ(rb_push(NULL, NULL, 0), RB_EINVAL);")],
    })
    fs = st.functions["rb_push"]
    c = fs.case("test_rb_push_null")
    assert c.status == CaseStatus.PASS and c.review_fixes_used == 1 and "rb_push(NULL, NULL, 0)" in c.body
    assert fs.review_notes == ["test_rb_push_null: meaningless_assertion — NULL만 확인해 반환값의 의미를 보지 않는다"]
    fx = fixer_prompts(p)[0]
    assert "Failure category: review_reject" in fx and "리뷰 검사: meaningless_assertion" in fx
    assert [c["tag"] for c in p.calls].count("reviewer") == 1  # 고친 케이스는 Reviewer에게 다시 가지 않는다
    assert "- 리뷰: test_rb_push_null: meaningless_assertion" in (orch.rundir.path / "final" / "report.md").read_text(encoding="utf-8")


def test_reviewer_finding_without_budget_drops_case(world):
    st, *_ = run(world, {
        "test_writer": [SMOKE, cases(("test_rb_push_null", NULL))],
        "reviewer": [review(("test_rb_push_null", "rule_violation", "규칙의 이름 형식을 어김"))],
    }, overrides=["budget.max_builds_per_run=4", "budget.final_build_reserve=1"])
    c = st.functions["rb_push"].case("test_rb_push_null")
    assert c.status == CaseStatus.DROPPED and c.drop_reason == "리뷰 지적(rule_violation): 규칙의 이름 형식을 어김"


def test_reviewer_ignores_unknown_cases(world):
    st, orch, _ = run(world, {"test_writer": [SMOKE], "reviewer": [review(("test_nope", "meaningless_assertion", "없는 케이스"))]})
    assert st.functions["rb_push"].case("test_rb_push_ok").status == CaseStatus.PASS and st.functions["rb_push"].review_notes == []
    assert any("지적 0건" in e["text"] for e in events(orch, "message"))
