import json
import math
import re

import pytest

from conftest import SAMPLE
from genut.agents.analyzer import Analyzer, AnalyzerOutput, judge_testability
from genut.agents.base import AgentEnv, AgentError, core_section, fit, load_prompt
from genut.agents.test_writer import TestWriter, identifier, used_helpers
from genut.analysis.compile_db import CompileDB
from genut.analysis.extractor import analyze_file
from genut.analysis.linkscope import GTestLinkContext, judge_calls
from genut.analysis.model import AnalysisReport, FunctionInfo, ReturnPoint
from genut.budget import Budget
from genut.config import GenutConfig
from genut.knowledge import KnowledgeStore
from genut.llm.fake_provider import FakeProvider
from genut.llm.prompt_builder import REQUIRED, SHRINKABLE, Fragment, PromptBuilder
from genut.llm.provider import TransportError
from genut.llm.tokens import TokenCounter
from genut.rules import Rules
from genut.state import CaseStatus, FunctionState, FunctionStatus, RunState, SharedArea, TestCase, text_hash
from genut.trace import RunDir, Trace

RULES = Rules(common_text="## 반드시\n- 테스트 이름: test_<함수명>_<상황>\n\n## 커버리지 최소\n- 정상 1, 오류 1, 경계 1")
SOURCE = "int rb_push(struct rb *rb, const void *data, size_t len)\n{\n\tif (rb == NULL)\n\t\treturn RB_EINVAL;\n\treturn RB_OK;\n}"
ANALYZED = {
    "testable": True,
    "reason": "",
    "cautions": ["rb는 rb_init으로 초기화한 뒤 써야 한다"],
    "recommended": [{"name": "test_rb_push_null_rb", "kind": "null", "purpose": "rb가 NULL", "expect": "RB_EINVAL"}],
}


def _fn(**kw):
    base = dict(
        name="rb_push", signature="int rb_push(struct rb *rb, const void *data, size_t len)", return_type="int",
        file="src/ring_buffer.c", start_line=26, end_line=31, source=SOURCE,
        returns=[ReturnPoint(line=29, expr="RB_EINVAL", condition="(rb == NULL) 참"), ReturnPoint(line=30, expr="RB_OK")],
        includes=['#include "rb.h"'], constants={"RB_OK": "0", "RB_EINVAL": "-22"},
    )
    base.update(kw)
    return FunctionInfo(**base)


def _report(**kw):
    return AnalysisReport(function=_fn(**kw))


def _case(name, body, status=CaseStatus.PASS, reason=""):
    return TestCase(name=name, target_function="rb_push", purpose=f"{name} 목적", body=body, status=status, drop_reason=reason)


def _cases(*items):
    return {"cases": [{"name": n, "purpose": "목적", "body": b} for n, b in items], "helpers": []}


@pytest.fixture
def trace(tmp_path):
    t = Trace(RunDir(tmp_path / "run"))
    yield t
    t.close()


def _env(provider, trace=None, home=None, backend="gtest", budget=None, **llm):
    cfg = GenutConfig.model_validate({"product": {"backend": backend}, "llm": {"provider": "fake", **llm}, "budget": budget or {}})
    return AgentEnv(provider=provider, counter=TokenCounter(), config=cfg, rules=RULES, home=home, trace=trace, budget=Budget(cfg.budget))


def _events(trace, type_):
    lines = trace.rundir.trace_jsonl.read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["type"] == type_]


# ------------------------------------------------------------------ prompts
def test_prompt_template_defaults_and_override(tmp_path):
    en = load_prompt("test_writer", "en")
    assert set(en.sections) == {"smoke", "expand", "example.gtest", "example.kunit"}
    assert "Test Writer" in en.preamble and en.sources == ["genut:prompts/en/test_writer.md"]
    assert "Test Writer입니다" in load_prompt("test_writer", "ko").preamble
    assert set(load_prompt("analyzer", "ko").sections) == {"task"}

    user = tmp_path / "prompts" / "test_writer.md"
    user.parent.mkdir()
    user.write_text("Custom role for $function <!-- 튜닝 메모 -->\n\n## smoke\nOnly one for $function.\n", encoding="utf-8")
    t = load_prompt("test_writer", "en", home=tmp_path)
    assert t.render(None, {"function": "f"}) == "Custom role for f"
    assert t.render("smoke", {"function": "f"}) == "Only one for f."
    assert t.sections["expand"] == en.sections["expand"]  # 사용자 파일에 없는 절은 기본값
    assert t.sources[0] == str(user) and t.digest() != en.digest()
    assert t.render("expand", {}).count("$existing") == 1  # 모르는 자리표시자는 그대로


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("backend", ["gtest", "kunit"])
@pytest.mark.parametrize("agent", [Analyzer, TestWriter])
def test_default_prompts_leave_no_placeholder(agent, backend, language):
    a = agent(_env(FakeProvider(), backend=backend, prompt_language=language))
    values = a.values("rb_push", existing="test_rb_push_ok")
    for section in [None, *a.prompt.sections]:
        text = a.prompt.render(section, values)
        assert text and not re.search(r"\$\{?\w", text), (section, text)
    assert "test_rb_push_<" in a.prompt.render(None, values)


def test_core_section_and_fit():
    md = "intro\n<!-- genut:core -->\nset_return(f, 0);\n<!-- /genut:core -->\ntail"
    assert core_section(md) == "set_return(f, 0);"
    assert core_section("no markers") == ""
    req = Fragment("req", "r" * 300, REQUIRED)
    shr = Fragment("shr", "s" * 300, SHRINKABLE, shrinkers=[lambda: "s" * 30, lambda: ""])
    out = fit([req, shr], 120, TokenCounter())
    assert out[0].text == req.text and out[1].text == "s" * 30
    assert shr.text == "s" * 300 and len(shr.shrinkers) == 2  # 원본은 그대로


def test_identifier_and_used_helpers():
    assert identifier("test rb_push 가득 참", "x") == "test_rb_push"
    assert identifier("1st", "test_f") == "test_f_1st" and identifier("한글", "test_f") == "test_f"
    helpers = {"make_rb": "void make_rb(void) {}", "fill_rb": "void fill_rb(void) { make_rb(); }", "other": "void other(void) {}"}
    assert used_helpers(["fill_rb();"], helpers) == ["fill_rb", "make_rb"]
    assert used_helpers(["make_rb_x();"], helpers) == []


# ----------------------------------------------------------------- analyzer
def test_analyzer_fills_state_and_records_call(trace):
    p = FakeProvider(script={"analyzer": [ANALYZED]})
    env = _env(p, trace)
    fs = FunctionState(name="rb_push")
    verdict = Analyzer(env).run(_report(), fs)
    assert verdict.testable and not verdict.warning
    assert fs.cautions == ANALYZED["cautions"] and fs.recommended[0]["expect"] == "RB_EINVAL"
    call = p.calls[0]
    assert call["tag"] == "analyzer" and call["schema"] is not None
    system, user = (m.content for m in call["messages"])
    assert "You are the Analyzer" in system and "test_<함수명>_<상황>" in system
    assert "int rb_push(struct rb *rb" in user and "## 반환 지점" in user and "Analyze `rb_push`" in user
    assert fs.tokens_used > 0 and env.budget.tokens["rb_push"] == fs.tokens_used
    assert (trace.rundir.path / "prompts" / "01-analyzer.md").exists()
    ev = _events(trace, "llm_call")[0]
    assert ev["agent"] == "analyzer" and ev["function"] == "rb_push" and ev["trims"] == [] and ev["path"] == "native"
    assert ev["prompt"] == "genut:prompts/en/analyzer.md" and ev["prompt_hash"]
    assert _events(trace, "testability")[0]["testable"] is True


def test_untestable_needs_a_decisive_signal():
    no = AnalyzerOutput(testable=False, reason="하드웨어 의존", cautions=[], recommended=[])
    v = judge_testability(no, _fn())
    assert v.testable and "신호가 없어 진행" in v.warning
    assert not judge_testability(no, _fn(), has_compile_entry=False).testable
    assert "빈 함수" in judge_testability(no, _fn(empty_body=True, returns=[])).reason

    p = FakeProvider(script={"analyzer": [{**ANALYZED, "testable": False, "reason": "어셈블리뿐"}]})
    fs = FunctionState(name="rb_push")
    v = Analyzer(_env(p)).run(_report(only_asm=True), fs)
    assert not v.testable and fs.status == FunctionStatus.UNTESTABLE
    assert fs.status_reason == "본문이 인라인 어셈블리뿐 — 어셈블리뿐"


# ------------------------------------------------------------- agent rules
def test_budget_and_overflow_do_not_call_llm(trace):
    p = FakeProvider()
    env = _env(p, trace)
    env.budget.add_tokens("rb_push", 10**9)
    fs = FunctionState(name="rb_push")
    with pytest.raises(AgentError) as e:
        Analyzer(env).run(_report(), fs)
    assert e.value.kind == "budget" and p.calls == []

    small = FakeProvider(max_context=500)  # 출력 예약(6000)만으로 한도가 0
    with pytest.raises(AgentError) as e:
        Analyzer(_env(small, trace)).run(_report(), fs)
    assert e.value.kind == "context_overflow" and small.calls == []
    assert _events(trace, "context_overflow")[0]["agent"] == "analyzer"
    assert [x["reason"] for x in _events(trace, "agent_skipped")] == ["budget"]


def test_small_context_drops_example_first(trace):
    fs = FunctionState(name="rb_push", cautions=["주의"], cases=[_case("test_rb_push_ok", "EXPECT_EQ(rb_push(&rb, d, 1), RB_OK);")])
    big = _env(FakeProvider(), budget={"analysis_token_budget": 10**6})
    frags = TestWriter(big).fragments("expand", _report(), fs)
    full = PromptBuilder(TokenCounter(), 10**9, 0, 0.0).build(frags).tokens
    reserve = big.config.llm.context.output_reserve_tokens
    p = FakeProvider(script={"test_writer": [_cases(("test_rb_push_null", "EXPECT_EQ(rb_push(NULL, d, 1), RB_EINVAL);"))]},
                     max_context=math.ceil((full - 5 + reserve) / 0.95))
    out = TestWriter(_env(p, trace, budget={"analysis_token_budget": 10**6})).write("expand", _report(), fs)
    assert [c.name for c in out.cases] == ["test_rb_push_null"]
    trims = _events(trace, "llm_call")[0]["trims"]
    assert trims[0]["name"] == "example" and trims[0]["action"] == "dropped"
    assert "sum(v, 2)" not in p.calls[0]["messages"][1].content


def test_parse_path_reserves_format_and_limits_repairs(trace):
    fs = FunctionState(name="rb_push")
    n = Analyzer(_env(FakeProvider(script={"analyzer": [ANALYZED]})))
    native = n.call(n.fragments(_report()), function="rb_push")
    p = FakeProvider(json_schema=False, script={"analyzer": ["설명만 있고 JSON이 없음", ANALYZED]})
    a = Analyzer(_env(p))
    res = a.call(a.fragments(_report()), function="rb_push")
    assert res.tokens == native.tokens
    assert res.path == "parse" and res.attempts == 2 and p.calls[0]["schema"] is None
    assert "JSON Schema" in p.calls[0]["messages"][-1].content
    fmt = native.limit - res.limit
    assert fmt > 0  # 출력 형식 안내 몫을 비워 둔다

    # 복구 요청이 들어갈 자리가 없으면 복구하지 않고 바로 실패한다
    reserve = a.env.config.llm.context.output_reserve_tokens
    tight = FakeProvider(json_schema=False, script={"analyzer": ["JSON 아님", ANALYZED]},
                         max_context=math.ceil((res.tokens + 50 + reserve + fmt) / 0.95))
    with pytest.raises(AgentError) as e:
        Analyzer(_env(tight, trace)).run(_report(), fs)
    assert e.value.kind == "invalid" and len(tight.calls) == 1
    assert _events(trace, "llm_call")[0]["error"] == "invalid" and fs.tokens_used > 0


def test_transport_error_is_infra_error(trace):
    def down(tag, messages, schema):
        raise TransportError("연결 끊김")

    with pytest.raises(AgentError) as e:
        Analyzer(_env(FakeProvider(responder=down), trace)).run(_report(), FunctionState(name="rb_push"))
    assert e.value.kind == "infra_error" and "연결 끊김" in _events(trace, "infra_error")[0]["error"]


# -------------------------------------------------------------- test writer
def test_smoke_keeps_one_case(trace):
    p = FakeProvider(script={"test_writer": [_cases(("test rb_push ok", "EXPECT_EQ(rb_push(&rb, d, 1), RB_OK);"),
                                                     ("test_rb_push_null", "EXPECT_EQ(rb_push(NULL, d, 1), RB_EINVAL);"))]})
    fs = FunctionState(name="rb_push", cautions=["rb 초기화 필요"])
    out = TestWriter(_env(p, trace)).write("smoke", _report(), fs, round_=1)
    assert [c.name for c in fs.cases] == ["test_rb_push_ok"]
    c = fs.cases[0]
    assert c.status == CaseStatus.NEW and c.origin == "smoke" and c.target_function == "rb_push"
    assert (c.history[0].round, c.history[0].stage) == (1, "smoke")
    assert any("하나만" in n for n in out.notes) and any("고쳤습니다" in n for n in out.notes)
    system, user = (m.content for m in p.calls[0]["messages"])
    assert "TEST(<suite>, <case name>)" in system and "## 공통 규칙" in system
    assert "## Analyzer 의견" in user and "rb 초기화 필요" in user
    assert "sum(v, 2)" in user and user.rstrip().endswith("Do not test error paths yet.")
    assert (trace.rundir.path / "prompts" / "01-test_writer-smoke.md").exists()
    assert _events(trace, "cases_written")[0]["cases"] == ["test_rb_push_ok"]


def test_expand_protects_passing_cases_and_shared_helpers():
    make = "static void make_rb(struct rb *rb) { rb_init(rb, buf, 8); }"
    fs = FunctionState(name="rb_push", shared=SharedArea(helpers={"make_rb": make}))
    ok_body = "struct rb rb;\nmake_rb(&rb);\nEXPECT_EQ(rb_push(&rb, \"a\", 1), RB_OK);"
    fs.cases += [_case("test_rb_push_ok", ok_body), _case("test_rb_push_bad", "EXPECT_EQ(1, 2);", CaseStatus.DROPPED, "기대값 오류")]
    reply = {
        "cases": [
            {"name": "test_rb_push_ok", "purpose": "덮어쓰기 시도", "body": "EXPECT_TRUE(true);"},
            {"name": "test_rb_push_bad", "purpose": "다시", "body": "EXPECT_EQ(rb_push(NULL, d, 1), RB_EINVAL);"},
            {"name": "test_rb_push_full", "purpose": "가득 참", "body": "struct rb rb;\nfill_rb(&rb);\nEXPECT_EQ(rb_push(&rb, \"a\", 1), RB_ENOSPC);"},
            {"name": "test_rb_push_empty", "purpose": "빈 본문", "body": "  \n"},
        ],
        "helpers": [
            {"name": "make_rb", "code": "static void make_rb(struct rb *rb) { }"},
            {"name": "fill_rb", "code": "static void fill_rb(struct rb *rb) { make_rb(rb); rb->tail = rb->cap; }"},
            {"name": "unused", "code": "static int unused(void) { return 0; }"},
        ],
    }
    p = FakeProvider(script={"test_writer": [reply]})
    out = TestWriter(_env(p)).write("expand", _report(), fs, round_=2)
    assert [c.name for c in out.cases] == ["test_rb_push_bad_2", "test_rb_push_full"]
    assert fs.case("test_rb_push_ok").body == ok_body and fs.case("test_rb_push_ok").status == CaseStatus.PASS
    assert fs.case("test_rb_push_full").helpers_used == ["fill_rb", "make_rb"] and fs.case("test_rb_push_bad_2").helpers_used == []
    assert out.helpers == {"fill_rb": reply["helpers"][1]["code"]}
    assert fs.shared.version == 1 and fs.shared.helpers["make_rb"] == make and len(fs.shared_history) == 1
    notes = "\n".join(out.notes)
    assert "통과 케이스와 이름이 같아" in notes and "이미 있는 헬퍼는 바꾸지 않습니다" in notes
    assert "unused: 쓰는 케이스가 없어" in notes and "본문이 비어" in notes
    user = p.calls[0]["messages"][1].content
    assert "## 통과 케이스 (글자 하나 바꾸지 말 것)" in user and ok_body in user
    assert "test_rb_push_bad: 기대값 오류" in user and "## 공유 헬퍼" in user and make in user
    assert "Existing case names (do not reuse): test_rb_push_ok, test_rb_push_bad" in user


def test_expand_without_new_helpers_keeps_shared_version():
    fs = FunctionState(name="rb_push")
    p = FakeProvider(script={"test_writer": [_cases(("test_rb_push_null", "EXPECT_EQ(rb_push(NULL, d, 1), RB_EINVAL);"))]})
    TestWriter(_env(p)).write("expand", _report(), fs)
    assert fs.shared.version == 0 and fs.shared_history == []


def test_writer_knowledge_bugs_stub_usage_and_passed_budget(tmp_path):
    home, pdir = tmp_path / "home", tmp_path / "home" / "products" / "alpha"
    ks = KnowledgeStore(home, pdir, "alpha")
    ks.global_md.parent.mkdir(parents=True)
    ks.global_md.write_text("# 공통 지식\n\n- 구조체는 지정 초기화로 만든다\n", encoding="utf-8")
    ks.add_suspected_bug("rb_push", "test_rb_push_zero_len", "len 0을 거부하지 않음", text_hash(SOURCE), "r1")
    ks.add_suspected_bug("rb_push", "test_rb_push_old", "옛 본문 기준", "deadbeef", "r0")
    stub = "# 스텁 도구\n긴 설명\n<!-- genut:core -->\nSTUB_RETURN(rb_alloc_slot, NULL);\n<!-- /genut:core -->\n"
    fs = FunctionState(name="rb_push", cases=[_case("test_rb_push_ok", "EXPECT_EQ(rb_push(&rb, d, 1), RB_OK); // " + "x" * 400)])
    p = FakeProvider(script={"test_writer": [_cases(("test_rb_push_null", "EXPECT_EQ(rb_push(NULL, d, 1), RB_EINVAL);"))]})
    TestWriter(_env(p, budget={"passed_cases_token_budget": 50})).write("expand", _report(), fs, stub_usage=stub, knowledge=ks)
    system, user = (m.content for m in p.calls[0]["messages"])
    assert "## 스텁 사용법" in system and "긴 설명" in system
    assert "구조체는 지정 초기화로 만든다" in user
    assert "test_rb_push_zero_len: len 0을 거부하지 않음" in user and "test_rb_push_old" not in user
    assert "- test_rb_push_ok: test_rb_push_ok 목적" in user and "x" * 400 not in user  # 예산을 넘으면 이름만


def test_writer_fenced_mode():
    reply = ('{"cases": [{"name": "test_rb_push_ok", "purpose": "정상"}], "helpers": []}\n'
             "```c\n// case: test_rb_push_ok\nEXPECT_EQ(rb_push(&rb, d, 1), RB_OK);\n```\n")
    p = FakeProvider(script={"test_writer": [reply]})
    fs = FunctionState(name="rb_push")
    TestWriter(_env(p, code_format="fenced")).write("smoke", _report(), fs)
    assert fs.cases[0].body == "EXPECT_EQ(rb_push(&rb, d, 1), RB_OK);"
    assert p.calls[0]["schema"] is None and "// case: <name>" in p.calls[0]["messages"][-1].content


def test_writer_korean_prompt_and_kunit_wrapper():
    p = FakeProvider(script={"test_writer": [_cases(("test_rb_push_ok", "KUNIT_EXPECT_EQ(test, rb_push(&rb, d, 1), RB_OK);"))]})
    TestWriter(_env(p, backend="kunit", prompt_language="ko")).write("smoke", _report(), FunctionState(name="rb_push"))
    system, user = (m.content for m in p.calls[0]["messages"])
    assert "Test Writer입니다" in system and "KUnit" in system and "static void <case name>(struct kunit *test)" in system
    assert "케이스를 정확히 하나만" in user and "KUNIT_EXPECT_EQ(test, sum(v, 2), 5);" in user


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        TestWriter(_env(FakeProvider())).write("coverage", _report(), FunctionState(name="rb_push"))


# ---------------------------------------------------------------- full flow
@pytest.mark.libclang
def test_analyze_smoke_expand_on_sample_project(trace, sample_db):
    rb = SAMPLE / "src" / "ring_buffer.c"
    fn = analyze_file(rb, CompileDB.load(sample_db).find(rb)).get("rb_push")
    report = AnalysisReport(function=fn, linkscope=judge_calls(fn, rb, "gtest", RULES, GTestLinkContext(product_root=SAMPLE, link_sources=[rb])))
    p = FakeProvider(script={
        "analyzer": [ANALYZED],
        "test_writer": [
            _cases(("test_rb_push_ok", "struct rb rb = {0};\nEXPECT_EQ(rb_push(&rb, \"a\", 1), RB_OK);")),
            _cases(("test_rb_push_null_rb", "EXPECT_EQ(rb_push(NULL, \"a\", 1), RB_EINVAL);"),
                   ("test_rb_push_full", "struct rb rb = {0};\nEXPECT_EQ(rb_push(&rb, \"a\", 100), RB_ENOSPC);")),
        ],
    })
    env = _env(p, trace)
    state = RunState(run_id=trace.rundir.run_id, source_file=str(rb), backend="gtest")
    fs = state.functions.setdefault("rb_push", FunctionState(name="rb_push", signature=fn.signature))
    assert Analyzer(env).run(report, fs).testable
    writer = TestWriter(env)
    writer.write("smoke", report, fs, round_=0)
    fs.cases[0].mark(CaseStatus.PASS, 0, "smoke")  # 검증은 6단계 백엔드가 한다
    writer.write("expand", report, fs, round_=1)
    assert [(c.name, c.status) for c in fs.cases] == [
        ("test_rb_push_ok", CaseStatus.PASS), ("test_rb_push_null_rb", CaseStatus.NEW), ("test_rb_push_full", CaseStatus.NEW)]

    expand_user = p.calls[2]["messages"][1].content
    assert "rb_alloc_slot  undefined → 스텁 필요" in expand_user and "test_rb_push_null_rb [null]" in expand_user
    assert 'EXPECT_EQ(rb_push(&rb, "a", 1), RB_OK);' in expand_user
    names = sorted(x.name for x in (trace.rundir.path / "prompts").iterdir())
    assert names == ["01-analyzer.md", "02-test_writer-smoke.md", "03-test_writer-expand.md"]
    state.save(trace.rundir.state_json)
    again = RunState.load(trace.rundir.state_json).functions["rb_push"]
    assert again.tokens_used == env.budget.tokens["rb_push"] and len(again.cases) == 3
