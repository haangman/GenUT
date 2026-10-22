import pytest

from genut.agents.base import AgentEnv
from genut.agents.fixer import Fixer, FixerOutput, apply_fix, failure_text
from genut.agents.test_writer import Helper
from genut.analysis.model import AnalysisReport, FunctionInfo, ReturnPoint
from genut.budget import Budget
from genut.config import GenutConfig
from genut.llm.fake_provider import FakeProvider
from genut.llm.tokens import TokenCounter
from genut.models import CrashBrief, Diagnostic, FailureInfo
from genut.rules import Rules
from genut.state import CaseStatus, FunctionState, FunctionStatus, SharedArea, TestCase
from genut.verify.classify import allowed_strategies
from genut.verify.review import assertions, copied_actual, justified, review_case

RULES = Rules(reject_patterns=[r"EXPECT_TRUE\(\s*true\s*\)", r"sleep\("])


def _case(body, **kw):
    return TestCase(name="test_f_x", target_function="f", body=body, **kw)


# ------------------------------------------------------------------ review
def test_assertions_parse_nested_args_and_kunit():
    a = assertions('EXPECT_EQ(f(g(1, 2), "a,b"), 3);\nKUNIT_EXPECT_EQ(test, f(1), -22);\nASSERT_TRUE(ok)')
    assert a == [("EXPECT_EQ", ['f(g(1, 2), "a,b")', "3"]), ("KUNIT_EXPECT_EQ", ["f(1)", "-22"]), ("ASSERT_TRUE", ["ok"])]


@pytest.mark.parametrize("body,check", [
    ("f(1);", "no_assertion"),
    ("EXPECT_EQ(f(1), f(1));", "always_true"),
    ("EXPECT_EQ(1, 2); f(0);", "always_true"),
    ("KUNIT_EXPECT_TRUE(test, true); f(0);", "always_true"),
    ("EXPECT_EQ(g(1), 3);", "target_not_called"),
    ("EXPECT_EQ(f(1), 3); sleep(1);", "rejected_pattern"),
])
def test_review_findings(body, check):
    assert check in {x.check for x in review_case(_case(body), "f", {}, RULES)}


def test_review_passes_good_case_and_counts_helpers():
    assert review_case(_case("EXPECT_EQ(f(1), 3);\nEXPECT_NE(f(2), x);"), "f", {}, RULES) == []
    helpers = {"call_f": "static int call_f(void) { return f(1); }"}
    assert review_case(_case("EXPECT_EQ(call_f(), 3);"), "f", helpers, RULES) == []  # 헬퍼를 거친 호출도 호출이다
    assert {x.check for x in review_case(_case("EXPECT_EQ(fx(1), 3);"), "f", {}, RULES)} == {"target_not_called"}


def test_copied_actual_detection_and_justification():
    fi = FailureInfo(category="assertion_failed", stage="run", expected="0", actual="-28", observed=["-28", "0"])
    c = _case("EXPECT_EQ(f(6), -28);", previous_body="EXPECT_EQ(f(6), 0);", last_failure=fi)
    assert copied_actual(c) == ["-28"]
    assert copied_actual(_case("EXPECT_EQ(f(6), RB_ENOSPC);", previous_body="EXPECT_EQ(f(6), 0);", last_failure=fi)) == []
    assert copied_actual(_case("EXPECT_EQ(f(6), -28);", previous_body="EXPECT_EQ(f(6), -28);", last_failure=fi)) == []  # 원래 있던 값
    assert justified(":34 RB_ENOSPC 반환", [31, 34]) and justified("34번 줄", [34]) and not justified("실제값이 -28이라서", [31, 34])
    assert not justified("134번 줄", [34])


# -------------------------------------------------------------- apply_fix
def _out(strategy="fix_case", body="EXPECT_EQ(f(1), 2);", **kw):
    data = {"strategy": strategy, "reason": "이유", "body": body, "evidence": "", "suspected_bug": False, "helpers": []}
    data.update(kw)
    return FixerOutput.model_validate(data)


def _fs(*cases):
    return FunctionState(name="f", cases=list(cases), shared=SharedArea(helpers={"mk": "static void mk(void) {}"}))


def test_apply_fix_case_and_disallowed_strategy():
    c = _case("EXPECT_EQ(f(1), 1);", status=CaseStatus.FAIL)
    fs = _fs(c)
    o = apply_fix(_out(evidence=":3"), fs, c, allowed_strategies("fix_case"), 2, False)
    assert o.retest and c.status == CaseStatus.NEW and c.previous_body == "EXPECT_EQ(f(1), 1);" and c.fix_evidence == ":3" and c.fixes_used == 1
    c2 = _case("x", status=CaseStatus.FAIL)
    o = apply_fix(_out("fix_case"), _fs(c2), c2, allowed_strategies("drop_case"), 2, False)
    assert o.strategy == "drop_case" and c2.status == CaseStatus.DROPPED and "허용되지 않은 전략" in o.notes[0]


def test_apply_fix_same_body_does_not_retest():
    c = _case("EXPECT_EQ(f(1), 2);", status=CaseStatus.FAIL)
    o = apply_fix(_out(), _fs(c), c, ["fix_case"], 1, False)
    assert not o.retest and c.status == CaseStatus.FAIL and c.fixes_used == 1


def test_apply_add_stub_without_tool_narrows_and_give_up_keeps_passes():
    c = _case("x", status=CaseStatus.FAIL)
    fs = _fs(c)
    o = apply_fix(_out("add_stub"), fs, c, allowed_strategies("add_stub"), 1, stub_supported=False)
    assert o.strategy == "narrow_scope" and c.status == CaseStatus.DROPPED and "스텁 도구 연동 필요" in fs.narrowed[0]
    ok, bad, new = _case("p", status=CaseStatus.PASS), _case("b", status=CaseStatus.FAIL), _case("n")
    ok.name, new.name = "test_ok", "test_new"
    fs = _fs(ok, bad, new)
    apply_fix(_out("give_up"), fs, bad, allowed_strategies("fix_case"), 3, False)
    assert fs.status == FunctionStatus.GIVEN_UP and ok.status == CaseStatus.PASS
    assert bad.status == CaseStatus.DROPPED and new.status == CaseStatus.DROPPED


def test_apply_drop_suspected_bug_and_helpers():
    c = _case("x", status=CaseStatus.FAIL)
    apply_fix(_out("drop_case", suspected_bug=True), _fs(c), c, ["drop_case"], 1, False)
    assert c.status == CaseStatus.SUSPECTED_BUG
    c = _case("x", status=CaseStatus.FAIL)
    fs = _fs(c)
    o = apply_fix(_out(helpers=[Helper(name="mk", code="static void mk(void) { setup(); }")]), fs, c, ["fix_case"], 1, False)
    assert o.shared_changed and fs.shared.version == 1 and "setup()" in fs.shared.helpers["mk"]
    c.shared_change_blocked = True
    c.status = CaseStatus.FAIL
    o = apply_fix(_out(body="EXPECT_EQ(f(9), 9);", helpers=[Helper(name="mk", code="static void mk(void) { other(); }")]), fs, c, ["fix_case"], 2, False)
    assert not o.shared_changed and fs.shared.version == 1 and "바꿀 수 없어" in o.notes[0]


# ------------------------------------------------------------------- Fixer
def test_failure_text_levels():
    fi = FailureInfo(category="crash", stage="run", summary="segv @ a.c:3", details="긴 상세", diagnostics=[Diagnostic(file="t.cc", line=3, message="m")],
                     crash=CrashBrief(category="segv", location="a.c:3"))
    full = failure_text(fi)
    assert "## 크래시 브리프" in full and "긴 상세" in full and "t.cc:3:0: error: m" in full
    assert "긴 상세" not in failure_text(fi, "brief") and "크래시 브리프" not in failure_text(fi, "summary")


def test_fixer_prompt_lists_allowed_strategies_and_keeps_passes():
    fn = FunctionInfo(name="f", signature="int f(int)", return_type="int", file="f.c", start_line=1, end_line=3, source="int f(int x)\n{\n\treturn x;\n}",
                      returns=[ReturnPoint(line=3, expr="x")])
    cfg = GenutConfig()
    p = FakeProvider(script={"fixer": [{"strategy": "drop_case", "reason": "r", "body": "", "evidence": "", "suspected_bug": False, "helpers": []}]})
    env = AgentEnv(p, TokenCounter(), cfg, Rules(common_text="## 반드시\n- 규칙"), budget=Budget(cfg.budget))
    bad = _case("EXPECT_EQ(f(1), 2);", status=CaseStatus.FAIL, last_failure=FailureInfo(category="assertion_failed", stage="run", summary="Which is: 1", expected="2", actual="1"))
    fs = FunctionState(name="f", cases=[_case("EXPECT_EQ(f(0), 0);", status=CaseStatus.PASS), bad])
    fs.cases[0].name = "test_f_zero"
    out, usage = Fixer(env).fix(AnalysisReport(function=fn), fs, bad, ["fix_case", "add_stub", "drop_case"])
    assert out.strategy == "drop_case" and usage.total > 0 and fs.tokens_used == usage.total
    system, user = (m.content for m in p.calls[0]["messages"])
    assert "You are the Fixer" in system and "## 반드시" in system
    assert "Allowed strategies (prefer the first): fix_case, add_stub, drop_case" in user and "Failure category: assertion_failed" in user
    assert "## 통과 케이스 (손대지 말 것)\n\ntest_f_zero" in user and "기대값: 2 · 실제값: 1" in user
    assert "must not change shared helpers" not in user
