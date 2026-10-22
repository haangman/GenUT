import subprocess

import pytest

from conftest import SAMPLE, SYSINC
from genut.analysis.parser import LibclangError
from genut.verify import staged, symbolize
from genut.verify.classify import Classifier, allowed_strategies
from genut.verify.diagnostics import LineMap, attribute, dedupe, link_symbol, parse_compiler_output
from genut.verify.logs.crash import CrashContext, build_brief
from genut.verify.logs.gtest_json import parse_gtest_stdout
from genut.verify.logs.ktap import parse_ktap
from genut.verify.logs.reduce import Part, assemble
from genut.verify.staged import cpp_args_from_c, semantic_check, werror_enabled

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


# ------------------------------------------------------------- semantic check
CHECK_ARGS = ["-I" + str(SAMPLE / "include"), "-isystem", str(SYSINC), "-std=gnu11"]


@pytest.mark.libclang
def test_semantic_check_ok_and_undeclared(tmp_path):
    p = tmp_path / "rb_test.c"
    assert semantic_check(p, '#include "rb.h"\nint f(void) { return 0; }\n', CHECK_ARGS).ok
    r = semantic_check(p, '#include "rb.h"\nint f(void) { return RB_NOPE; }\n', CHECK_ARGS)
    assert r.available and not r.ok
    assert [(d.severity, d.stage, d.line) for d in r.diagnostics] == [("error", "check", 2)]
    assert Classifier().classify_diagnostic(r.diagnostics[0]).category == "undeclared_identifier"


@pytest.mark.libclang
def test_semantic_check_missing_header_is_error(tmp_path):
    # libclang은 헤더 없음을 fatal로 낸다. 오류로 잡혀야 빌드 전에 걸러진다
    r = semantic_check(tmp_path / "rb_test.c", '#include "nope.h"\nint f(void) { return 0; }\n', CHECK_ARGS)
    assert r.available and not r.ok
    assert r.diagnostics[0].severity == "error" and "nope.h" in r.diagnostics[0].message
    assert Classifier().classify_diagnostic(r.diagnostics[0]).category == "missing_include"


@pytest.mark.libclang
def test_semantic_check_unavailable_without_framework_headers(tmp_path):
    r = semantic_check(tmp_path / "rb_test.cc", "#include <gtest/gtest.h>\nint f() { return 0; }\n", cpp_args_from_c(CHECK_ARGS))
    assert not r.available and r.ok and "gtest/gtest.h" in r.reason


@pytest.mark.libclang
def test_semantic_check_werror(tmp_path):
    p = tmp_path / "rb_test.c"
    text = '#include "rb.h"\nint f(void) { int tmp; return 0; }\n'
    args = CHECK_ARGS + ["-Wunused-variable"]
    assert semantic_check(p, text, args).ok
    r = semantic_check(p, text, args, werror=True)
    assert not r.ok and r.diagnostics[0].flag == "Werror=unused-variable"


def test_semantic_check_parse_failure_is_unavailable(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise LibclangError("libclang 없음")

    monkeypatch.setattr(staged, "parse", boom)
    r = semantic_check(tmp_path / "t.c", "", [])
    assert not r.available and r.ok and r.reason == "libclang 없음"


def test_werror_enabled():
    assert werror_enabled(["-O2", "-Werror"])
    assert not werror_enabled(["-Werror=unused-variable"])
    assert werror_enabled([], "CONFIG_FOO=y\nCONFIG_WERROR=y\n")
    assert not werror_enabled([], "# CONFIG_WERROR is not set\n")


def test_cpp_args_from_c():
    args = ["-Iinc", "-std=gnu11", "-Wstrict-prototypes", "-DX=1", "-Wall"]
    assert cpp_args_from_c(args) == ["-x", "c++", "-std=c++17", "-Iinc", "-DX=1", "-Wall"]
    assert cpp_args_from_c([], std="gnu++20") == ["-x", "c++", "-std=gnu++20"]


# ------------------------------------------------------------------ symbolize
def _run_returning(stdout, calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    return run


def _kernel_tree(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "faddr2line").write_text("#!/bin/sh\n")
    (tmp_path / "vmlinux").write_bytes(b"")
    return tmp_path, tmp_path / "vmlinux"


def test_faddr2line(tmp_path, monkeypatch):
    src, vmlinux = _kernel_tree(tmp_path)
    calls = []
    monkeypatch.setattr(symbolize.subprocess, "run", _run_returning("rb_push+0x2c/0x80:\nrb_push at drivers/foo/rb.c:42\n", calls))
    assert symbolize.faddr2line(src, vmlinux, "rb_push+0x2c/0x80") == "drivers/foo/rb.c:42"
    cmd, kw = calls[0]
    assert cmd == [str(src / "scripts" / "faddr2line"), str(vmlinux), "rb_push+0x2c/0x80"] and kw["cwd"] == str(src)
    monkeypatch.setattr(symbolize.subprocess, "run", _run_returning("no match\n"))
    assert symbolize.faddr2line(src, vmlinux, "rb_push+0x2c/0x80") is None


def test_faddr2line_unavailable(tmp_path, monkeypatch):
    assert symbolize.faddr2line(tmp_path, tmp_path / "vmlinux", "f+0x1/0x2") is None  # 스크립트·vmlinux 없음
    src, vmlinux = _kernel_tree(tmp_path)

    def fail(*a, **kw):
        raise subprocess.TimeoutExpired("faddr2line", 30)

    monkeypatch.setattr(symbolize.subprocess, "run", fail)
    assert symbolize.faddr2line(src, vmlinux, "f+0x1/0x2") is None


def test_addr2line(tmp_path, monkeypatch):
    binary = tmp_path / "rb_test"
    binary.write_bytes(b"")
    monkeypatch.setattr(symbolize.shutil, "which", lambda name: "/usr/bin/addr2line" if name == "addr2line" else None)
    calls = []
    monkeypatch.setattr(symbolize.subprocess, "run", _run_returning("/src/rb.c:42 (discriminator 1)\n", calls))
    assert symbolize.addr2line(binary, "0x401a2c") == "/src/rb.c:42"
    assert calls[0][0] == ["/usr/bin/addr2line", "-e", str(binary), "0x401a2c"]
    monkeypatch.setattr(symbolize.subprocess, "run", _run_returning("??:0\n"))
    assert symbolize.addr2line(binary, "0x401a2c") is None
    assert symbolize.addr2line(tmp_path / "missing", "0x1") is None
    monkeypatch.setattr(symbolize.shutil, "which", lambda name: None)
    assert symbolize.addr2line(binary, "0x401a2c") is None
