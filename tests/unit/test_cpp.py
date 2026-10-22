"""C++ 멤버 함수 (설계 13.1 12단계): 생성자·멤버 상태 추출, 호출 방법, gtest fixture 객체, 선택·차단 규칙."""

import json
from pathlib import Path

import pytest

from conftest import FIXTURES
from fake_backend import MarkerBackend
from genut.analysis.compile_db import CompileDB
from genut.analysis.extractor import analyze_file
from genut.analysis.model import AnalysisReport, ClassInfo, FunctionInfo
from genut.analysis.summarizer import call_hint, report_fragments
from genut.backends.base import TestUnit
from genut.backends.gtest import skeleton
from genut.context import make_context
from genut.llm.fake_provider import FakeProvider
from genut.orchestrator import GenOptions, Orchestrator, cpp_blocker
from genut.paths import ensure_home_layout
from genut.products import add_product
from genut.rules import Rules
from genut.state import CaseStatus, FunctionState, FunctionStatus, TestCase
from genut.verify.review import review_case

CPP = FIXTURES / "cpp_project"
SRC = CPP / "src" / "counter.cpp"


def cpp_db(tmp_path: Path) -> Path:
    p = tmp_path / "cc.json"
    p.write_text(json.dumps([{"directory": str(CPP), "file": "src/counter.cpp", "arguments": ["c++", "-Iinclude", "-std=c++17", "-c", "src/counter.cpp"]}]),
                 encoding="utf-8")
    return p


@pytest.fixture
def fa(tmp_path):
    return analyze_file(SRC, CompileDB.load(cpp_db(tmp_path)).find(SRC))


# ------------------------------------------------------------------ 추출
@pytest.mark.libclang
def test_extracts_members_owner_and_keys(fa):
    assert not fa.errors
    by = {f.key: f for f in fa.functions}
    assert set(by) == {"add_clamped", "Counter_Counter", "Counter_Counter_2", "Counter_add", "Counter_value", "Counter_reset", "Counter_valid_limit",
                       "Counter_check", "Shape_twice", "box_get"}
    add = by["Counter_add"]
    assert (add.display, add.qualified_name, add.access, add.is_method, add.is_const) == ("Counter::add", "demo::Counter::add", "public", True, False)
    assert add.owner.qualified_name == "demo::Counter" and add.owner.constructors == ["Counter()", "Counter(int limit)"] and add.owner.default_constructible
    assert add.owner.fields == ["int value_ (private)", "int limit_ (private)"]
    assert add.owner.methods == ["int add(int n)", "int value() const", "void reset()", "static bool valid_limit(int limit)"]
    assert add.fixture_object == "demo::Counter" and add.includes == ['#include "counter.h"']
    assert by["Counter_value"].is_const and by["Counter_valid_limit"].is_static_method and by["Counter_valid_limit"].fixture_object == ""
    assert by["Counter_Counter"].is_constructor and by["Counter_check"].access == "private"
    assert by["Shape_twice"].owner.abstract and by["Shape_twice"].fixture_object == ""  # 추상 클래스는 기본 생성할 수 없다
    assert by["add_clamped"].namespace == "demo" and not by["add_clamped"].is_method
    assert [f.key for f in fa.select("Counter::Counter")] == ["Counter_Counter", "Counter_Counter_2"]  # 오버로드는 모두
    assert fa.get("demo::Counter::add") is add and fa.get("Counter_add") is add and fa.get("add") is add


@pytest.mark.libclang
def test_report_tells_how_to_call(fa):
    add = fa.get("Counter::add")
    frags = {f.name: f for f in report_fragments(AnalysisReport(function=add))}
    assert "public 멤버 함수 of demo::Counter" in frags["analysis.target"].text
    assert "fixture가 기본 생성한 객체 `obj`를 준다(`obj.add(...)`)" in frags["analysis.call"].text
    assert "공개 생성자: Counter() · Counter(int limit)" in frags["analysis.class"].text and "멤버 변수" in frags["analysis.class"].text
    assert "멤버 변수" not in frags["analysis.class"].shrinkers[0]()
    assert "`demo::Counter::valid_limit(...)`로 부른다" in call_hint(fa.get("Counter::valid_limit"))
    assert "생성자다" in call_hint(fa.get("Counter_Counter_2"))
    assert "`demo::add_clamped(...)`로 부른다" in call_hint(fa.get("add_clamped"))
    assert "공개 생성자로 객체를 만든 뒤" in call_hint(fa.get("Shape::twice"))


def _fn(**kw):
    owner = ClassInfo(name="Counter", qualified_name="demo::Counter", constructors=["Counter()"], default_constructible=True)
    base = dict(name="add", key="Counter_add", qualified_name="demo::Counter::add", signature="int Counter::add(int n)", return_type="int",
                file=str(SRC), start_line=17, end_line=25, is_method=True, class_name="Counter", namespace="demo", access="public", owner=owner,
                includes=['#include "counter.h"'])
    base.update(kw)
    return FunctionInfo(**base)


def test_gtest_fixture_object_and_symbol():
    u = TestUnit(source=SRC, report=AnalysisReport(function=_fn()), state=FunctionState(name="Counter_add",
                 cases=[TestCase(name="test_Counter_add_ok", target_function="Counter_add", body="EXPECT_EQ(obj.add(1), 1);")]))
    r = skeleton.render(u, "counter_Counter_add_test.cc")
    assert 'extern "C"' not in r.text and "class CounterCounterAddTest : public ::testing::Test {\n protected:\n  demo::Counter obj;\n  void SetUp()" in r.text
    assert r.text.startswith("// GenUT가 생성한 테스트입니다: Counter::add (counter.cpp)") and r.symbol == "demo::Counter::add"
    static = TestUnit(source=SRC, report=AnalysisReport(function=_fn(name="valid_limit", is_static_method=True)), state=FunctionState(name="Counter_valid_limit"))
    assert " obj;" not in skeleton.render(static, "x_test.cc").text


def test_review_recognizes_member_and_constructor_calls():
    def found(body, name, **kw):
        return {f.check for f in review_case(TestCase(name="t", target_function="x", body=body), name, {}, Rules(), **kw)}

    assert "target_not_called" in found("EXPECT_EQ(obj.add(1), 1);", "add")  # 자유 함수 기준이면 다른 객체의 멤버 호출로 본다
    assert "target_not_called" not in found("EXPECT_EQ(obj.add(1), 1);", "add", member=True)
    assert "target_not_called" not in found("EXPECT_TRUE(demo::Counter::valid_limit(1));", "valid_limit", member=True)
    assert "target_not_called" not in found("demo::Counter c(3);\nEXPECT_EQ(c.value(), 0);", "Counter", member=True, constructor=True)


def test_cpp_blockers():
    assert "private 멤버 함수" in cpp_blocker(_fn(access="private"))
    assert cpp_blocker(_fn(access="private"), stub="real") == ""  # 스텁 도구가 있으면 그 접근 방식을 쓴다
    assert "템플릿" in cpp_blocker(_fn(is_template=True))
    assert "추상 클래스" in cpp_blocker(_fn(owner=ClassInfo(name="Shape", qualified_name="demo::Shape", abstract=True)))
    assert cpp_blocker(_fn(is_static_method=True, owner=ClassInfo(name="Shape", qualified_name="demo::Shape", abstract=True))) == ""
    assert cpp_blocker(_fn()) == ""


# ------------------------------------------------------- 오케스트레이터
@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    ensure_home_layout(home)
    add_product(home, "cpp", CPP, "gtest")
    return home, MarkerBackend(cpp_db(tmp_path), tmp_path / "installed", [SRC])


@pytest.mark.libclang
def test_default_selection_and_member_flow(world):
    home, be = world

    def responder(tag, messages, schema):
        if tag == "test_writer":
            return {"cases": [{"name": "test_Counter_add_ok", "purpose": "더하기", "body": "EXPECT_EQ(obj.add(3), 3);"}], "helpers": []}
        return None

    p = FakeProvider(responder=responder)
    orch = Orchestrator(make_context(home, "cpp"), SRC, GenOptions(functions=["Counter::add", "Counter::check", "Counter::Counter"]), provider=p, backend=be)
    st = orch.run()
    assert list(st.functions) == ["Counter_add", "Counter_check", "Counter_Counter", "Counter_Counter_2"]
    add = st.functions["Counter_add"]
    assert add.case("test_Counter_add_ok").status == CaseStatus.PASS and add.final_check == "ok"
    assert st.functions["Counter_check"].status == FunctionStatus.UNTESTABLE
    writer = next(c for c in p.calls if c["tag"] == "test_writer")["messages"]
    assert "Every case calls `Counter::add`" in writer[0].content and "test_Counter_add_<situation>" in writer[0].content
    assert "## 호출 방법" in writer[1].content and "demo::Counter obj;" in be.installed["Counter_add"]
    assert orch.budget.tokens.get("Counter_add", 0) > 0 and "add" not in orch.budget.tokens  # 예산은 함수 키로
    assert (orch.rundir.path / "analysis" / "Counter_add.json").exists()

    default = Orchestrator(make_context(home, "cpp"), SRC, GenOptions(dry_run=True), backend=be).run()
    assert set(default.functions) == {"add_clamped", "Counter_add", "Counter_value", "Counter_reset", "Counter_valid_limit", "Shape_twice", "box_get"}
