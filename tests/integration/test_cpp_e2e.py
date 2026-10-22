"""C++ 멤버 함수를 실제 cmake·컴파일러·gtest로 끝까지 (설계 13.1 12단계 e2e).

인스턴스 멤버 함수는 fixture가 기본 생성한 obj로, static 멤버와 namespace 함수는 완전한 이름으로 부른다.
private 멤버는 스텁 도구 없이 부를 수 없어 테스트 불가로 끝난다.
"""

import shutil

import pytest

from conftest import FIXTURES
from genut.context import make_context
from genut.llm.fake_provider import FakeProvider
from genut.orchestrator import GenOptions, Orchestrator
from genut.paths import ensure_home_layout
from genut.products import add_product
from genut.state import FunctionStatus

pytestmark = [pytest.mark.integration, pytest.mark.libclang]


def _cases(name, body):
    return {"cases": [{"name": name, "purpose": f"{name} 확인", "body": body}], "helpers": []}


def responder(tag, messages, schema):
    if tag != "test_writer":
        return None
    user = messages[-1].content
    smoke, coverage = "Write exactly one" in user, "## 미커버" in user
    if "`Counter::add`" in user:
        if smoke:
            return _cases("test_Counter_add_ok", "EXPECT_EQ(obj.add(3), 3);\nEXPECT_EQ(obj.value(), 3);")
        if coverage:
            return _cases("test_Counter_add_negative", "EXPECT_EQ(obj.add(-1), -1);")
        return _cases("test_Counter_add_over", "demo::Counter c(2);\nEXPECT_EQ(c.add(5), -1);")
    if "`Counter::valid_limit`" in user:
        return _cases("test_Counter_valid_limit_pos", "EXPECT_TRUE(demo::Counter::valid_limit(5));\nEXPECT_FALSE(demo::Counter::valid_limit(0));")
    if "`add_clamped`" in user:
        if smoke:
            return _cases("test_add_clamped_low", "EXPECT_EQ(demo::add_clamped(1, 2, 10), 3);")
        return _cases("test_add_clamped_high", "EXPECT_EQ(demo::add_clamped(8, 9, 10), 10);")
    return None


def test_cpp_members_end_to_end(tmp_path):
    root, home = tmp_path / "proj", tmp_path / "home"
    shutil.copytree(FIXTURES / "cpp_project", root)
    ensure_home_layout(home)
    add_product(home, "cpp", root, "gtest")
    funcs = ["Counter::add", "Counter::valid_limit", "add_clamped", "Counter::check"]
    st = Orchestrator(make_context(home, "cpp", overrides=["coverage.enabled=true"]), root / "src" / "counter.cpp", GenOptions(functions=funcs), provider=FakeProvider(responder=responder)).run()

    add = st.functions["Counter_add"]
    assert (add.result, add.final_check, add.coverage.percent) == ("complete", "ok", 100.0)
    assert {c.name for c in add.cases if c.origin == "coverage"} == {"test_Counter_add_negative"}
    assert st.functions["Counter_valid_limit"].result == "complete"
    clamped = st.functions["add_clamped"]
    assert clamped.result == "complete" and clamped.coverage.percent == 100.0
    check = st.functions["Counter_check"]
    assert check.status == FunctionStatus.UNTESTABLE and "private 멤버 함수" in check.status_reason

    gen = root / "tests" / "generated"
    text = (gen / "counter_Counter_add_test.cc").read_text(encoding="utf-8")
    assert "  demo::Counter obj;" in text and 'extern "C"' not in text and text.count("TEST_F(CounterCounterAddTest, ") == 3
    assert " obj;" not in (gen / "counter_Counter_valid_limit_test.cc").read_text(encoding="utf-8")
    assert not (gen / "counter_Counter_check_test.cc").exists()
