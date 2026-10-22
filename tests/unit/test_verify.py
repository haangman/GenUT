from genut.verify.classify import Classifier, allowed_strategies
from genut.verify.diagnostics import LineMap, attribute, dedupe, link_symbol, parse_compiler_output
from genut.verify.logs.crash import CrashContext, build_brief
from genut.verify.logs.gtest_json import parse_gtest_stdout
from genut.verify.logs.ktap import parse_ktap
from genut.verify.logs.reduce import Part, assemble

GCC = """t.cc: In member function 'virtual void S_test_full_Test::TestBody()':
t.cc:23:45: error: 'RB_MAX' was not declared in this scope
   23 |   EXPECT_EQ(rb_push(&rb, data, RB_MAX), RB_ENOSPC);
t.cc:23:45: error: 'RB_MAX' was not declared in this scope
t.cc:24:3: error: expected ';' before '}' token
t.cc:31:7: error: unused variable 'tmp' [-Werror=unused-variable]
"""
LD = """/usr/bin/ld: t.o: in function `S_test_ok_Test::TestBody()':
t.cc:(.text+0x5a): undefined reference to `rb_alloc_slot'
ld.lld: error: undefined symbol: foo_hw_reset
"""
KTAP = """[    1.0]     KTAP version 1
[    1.0]     # Subtest: other
[    1.0]     ok 1 test_other
[    1.0] ok 1 other
[    1.1]     KTAP version 1
[    1.1]     # Subtest: rb_push_test
[    1.1]     1..3
[    1.1]     ok 1 test_ok
[    1.2]     # test_full: EXPECTATION FAILED at x.c:34
[    1.2]     Expected rb_push(&rb, data, 8) == -28, but
[    1.2]         rb_push(&rb, data, 8) == 0 (0x0)
[    1.2]     not ok 2 test_full
[    1.3]     # test_loop: try timed out
[    1.3]     not ok 3 test_loop
[    1.4] not ok 2 rb_push_test
"""
OOPS = """[ 1.1] BUG: kernel NULL pointer dereference, address: 0000000000000008
[ 1.1] RIP: 0010:rb_push+0x2c/0x80
[ 1.1] RAX: 0000000000000000 RBX: 0000000000000008 RCX: 0000000000000000
[ 1.1] Call Trace:
[ 1.1]  ? __die+0x20/0x70
[ 1.1]  rb_push+0x2c/0x80
[ 1.1]  test_null_slot+0x5e/0x120 [rb_kunit]
[ 1.1]  kunit_try_run_case+0x7a/0x140
"""


def test_parse_and_dedupe_and_attribute():
    d = parse_compiler_output(GCC)
    assert len(d) == 4 and d[0].message.startswith("'RB_MAX'") and "S_test_full_Test" in d[0].notes[0]
    assert d[3].flag == "Werror=unused-variable"
    kept = dedupe(d)
    assert len(kept) == 2  # 중복 1건과 연쇄 1건 제거, -Werror 경고는 유지
    m = LineMap(path="t.cc", cases={"test_full": (20, 26)})
    owners = [x.owner for x in attribute(kept, [m])]
    assert owners == ["test_full", "@shared"]
    links = parse_compiler_output(LD, stage="link")
    assert [link_symbol(x) for x in links] == ["rb_alloc_slot", "foo_hw_reset"]
    lm = LineMap(path="t.cc", cases={"test_ok": (1, 5)}, function_names={"S_test_ok_Test": "test_ok"})
    assert attribute(links[:1], [lm])[0].owner == "test_ok"


def test_classifier():
    c = Classifier()
    d = parse_compiler_output(GCC)
    assert c.classify_diagnostic(d[0]).category == "undeclared_identifier"
    assert c.classify_diagnostic(d[3]).category == "warning_as_error"
    link = parse_compiler_output(LD, stage="link")[0]
    assert c.classify_diagnostic(link).strategy == "add_stub"
    assert c.classify_run("timeout").strategy == "drop_case"
    assert allowed_strategies("add_stub") == ["add_stub", "drop_case", "narrow_scope", "give_up"]


def test_ktap_suite_filter_and_results():
    s = parse_ktap(KTAP, {"rb_push_test"})
    assert list(s) == ["rb_push_test"]
    r = s["rb_push_test"].results
    assert r["test_ok"].status == "pass" and r["test_full"].status == "fail" and r["test_loop"].status == "timeout"
    assert r["test_full"].line == 34 and "0" in r["test_full"].observed


def test_gtest_stdout_crash_suspect():
    out = "[ RUN      ] S.test_ok\n[       OK ] S.test_ok (0 ms)\n[ RUN      ] S.test_crash\nSegmentation fault\n"
    res, running = parse_gtest_stdout(out)
    assert res["test_ok"].status == "pass" and running == "test_crash"


def test_crash_brief_kernel():
    ctx = CrashContext(test_functions={"test_null_slot"}, target_functions={"rb_push"})
    b = build_brief(OOPS, ctx, suspect_case="test_null_slot")
    assert b.category == "null_deref" and b.location == "rb_push"
    assert b.chain[0].startswith("test_null_slot") and "rb_push" in b.chain[-1]
    assert "## 크래시 브리프" in b.render()


def test_reduce_keeps_priority_one():
    parts = [Part(1, "결과", "ok 1 a\nnot ok 2 b"), Part(4, "크래시 직전 꼬리", "x" * 3000), Part(2, "실패", "y" * 300)]
    text = assemble(parts, budget=300)
    assert "ok 1 a" in text and "실패" in text and len(text) < 3000
