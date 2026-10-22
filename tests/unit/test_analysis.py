import json

import pytest

from conftest import SAMPLE, SYSINC, write_compile_db
from genut.analysis.compile_db import CompileDB, CompileEntry, guess_target, load_kernel_cmd
from genut.analysis.extractor import analyze_file
from genut.analysis.flag_sanitizer import FlagSanitizer
from genut.analysis.linkscope import GTestLinkContext, KernelLinkContext, judge_calls
from genut.analysis.model import AnalysisReport, Scope
from genut.analysis.summarizer import numbered_source, render_report, report_fragments
from genut.rules import Rules

RB = SAMPLE / "src" / "ring_buffer.c"


def _line_of(path, needle):
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if needle in line:
            return i
    raise AssertionError(needle)


def test_compile_db_find_and_clean(sample_db):
    db = CompileDB.load(sample_db)
    e = db.find(RB)
    assert e is not None
    args = e.clean_args()
    assert "-c" not in args and "-o" not in args and not any(a.endswith("ring_buffer.c") for a in args)
    assert args[0] == "-I" + str((SAMPLE / "include").resolve())
    assert str(SYSINC) in args
    assert db.neighbor(SAMPLE / "src" / "ring_buffer_rb_push_test.c").file.parent == RB.parent
    assert db.neighbor(SAMPLE / "tests" / "x_test.cc") is not None


def test_command_string_entries(tmp_path):
    p = tmp_path / "cc.json"
    p.write_text(json.dumps([{"directory": str(SAMPLE), "command": "gcc -Iinclude -DX=1 -c src/slot.c -o slot.o", "file": "src/slot.c"}]))
    e = CompileDB.load(p).find(SAMPLE / "src" / "slot.c")
    assert "-DX=1" in e.clean_args()


def test_kernel_cmd_fallback(tmp_path):
    d = tmp_path / "drivers" / "foo"
    d.mkdir(parents=True)
    (d / "foo.c").write_text("int x;\n")
    (d / ".foo.o.cmd").write_text("savedcmd_drivers/foo/foo.o := gcc -nostdinc -Iinclude -D__KERNEL__ -c -o drivers/foo/foo.o drivers/foo/foo.c\n\nsource_x := y\n")
    e = load_kernel_cmd(d / "foo.c", tmp_path)
    assert e is not None and "-D__KERNEL__" in e.clean_args()


def test_guess_target():
    e = CompileEntry(file=RB, directory=SAMPLE, arguments=["aarch64-linux-gnu-gcc", "-c", "x.c"], compiler="aarch64-linux-gnu-gcc")
    assert guess_target(e, kernel=True) == "aarch64-linux-gnu"
    e2 = CompileEntry(file=RB, directory=SAMPLE, arguments=["gcc", "-m64"], compiler="gcc")
    assert guess_target(e2, kernel=True) == "x86_64-linux-gnu"
    assert guess_target(e2, kernel=False) is None


def test_flag_sanitizer(tmp_path):
    learned = tmp_path / "learned.yaml"
    s = FlagSanitizer(learned)
    out, dropped = s.sanitize(["-I/x", "-mno-fp-ret-in-387", "-fconserve-stack", "--param", "asan-stack=1", "-fplugin=foo.so", "-O2"])
    assert out[:2] == ["-I/x", "-O2"] and "-Wno-unknown-warning-option" in out
    assert set(dropped) == {"-mno-fp-ret-in-387", "-fconserve-stack", "--param", "asan-stack=1", "-fplugin=foo.so"}
    assert FlagSanitizer.unknown_from(["unknown argument: '-fweird'"]) == ["-fweird"]
    s.learn(["-fweird"])
    assert "-fweird" in FlagSanitizer(learned).remove


@pytest.fixture
def rb_analysis(sample_db):
    db = CompileDB.load(sample_db)
    return analyze_file(RB, db.find(RB))


@pytest.mark.libclang
def test_extract_rb_push(rb_analysis):
    fa = rb_analysis
    assert not fa.errors, fa.errors
    names = [f.name for f in fa.functions]
    assert names == ["rb_free_space", "rb_init", "rb_push", "rb_used", "rb_state_name"]  # RB_DEBUG가 없어 rb_dump는 빠짐
    fn = fa.get("rb_push")
    assert fn.signature == "int rb_push(struct rb *rb, const void *data, size_t len)"
    assert [p.name for p in fn.params] == ["rb", "data", "len"]
    assert fn.params[0].record == "struct rb"
    assert not fn.is_static and fa.get("rb_free_space").is_static
    calls = {c.name: c for c in fn.calls}
    assert set(calls) == {"rb_free_space", "rb_alloc_slot", "memcpy"}
    assert calls["rb_free_space"].def_file.endswith("ring_buffer.c") and calls["rb_free_space"].is_static
    assert calls["rb_alloc_slot"].def_file == "" and calls["rb_alloc_slot"].decl_file.endswith("rb.h")
    g = {x.name: x for x in fn.globals}
    assert g["stats"].access == "write" and g["stats"].is_static
    rets = [(r.expr, r.condition) for r in fn.returns]
    assert ("RB_EINVAL", "(rb == NULL || data == NULL) 참") in rets
    assert ("RB_ENOSPC", "(rb_free_space(rb) < len) 참") in rets
    assert rets[-1] == ("RB_OK", "")
    assert fn.constants["RB_ENOSPC"] == "-28" and "NULL" in fn.constants
    types = {t.name: t for t in fn.types}
    assert "struct rb" in types and "size_t head" in types["struct rb"].fields
    assert types["struct rb"].example_init.startswith("struct rb x = { .buf = NULL")
    assert fn.includes == ['#include "rb.h"', '#include <stddef.h>']  # 선언 헤더 + 본문이 쓰는 매크로(NULL)를 정의한 헤더
    full_line = _line_of(RB, "stats.full_count++")
    nomem_line = _line_of(RB, "return RB_ENOMEM")
    assert fn.reach(full_line).condition == "(rb_free_space(rb) < len) 참"
    assert fn.reach(nomem_line).condition == "(slot == NULL) 참"


@pytest.mark.libclang
def test_extract_switch_and_loops(sample_db):
    db = CompileDB.load(sample_db)
    fa = analyze_file(RB, db.find(RB))
    fn = fa.get("rb_state_name")
    kinds = [(b.kind, b.condition) for b in fn.branches]
    assert ("case", "switch (state) == RB_STATE_EMPTY") in kinds
    assert ("default", "switch (state) 기본(default)") in kinds
    assert [r.condition for r in fn.returns] == ["switch (state) == RB_STATE_EMPTY", "switch (state) == RB_STATE_FULL", "switch (state) 기본(default)"]
    su = SAMPLE / "src" / "str_util.c"
    fa2 = analyze_file(su, db.find(su))
    cc = fa2.get("str_count_char")
    assert any(b.kind == "loop" and "*s" in b.condition for b in cc.branches)
    inc = _line_of(su, "n++;")
    assert cc.reach(inc).condition == "(*s == c) 참"


@pytest.mark.libclang
def test_preproc_and_defines(tmp_path):
    db = CompileDB.load(write_compile_db(tmp_path / "cc.json", extra=["-DRB_DEBUG"]))
    fa = analyze_file(RB, db.find(RB))
    dump = fa.get("rb_dump")
    assert dump is not None and dump.preproc == ["defined(RB_DEBUG)"]


@pytest.mark.libclang
def test_unknown_flag_retry(tmp_path):
    learned = tmp_path / "l.yaml"
    db = CompileDB.load(write_compile_db(tmp_path / "cc.json", extra=["-fgenut-unknown-flag"]))
    fa = analyze_file(RB, db.find(RB), kernel=True, sanitizer=FlagSanitizer(learned))
    assert "-fgenut-unknown-flag" in fa.learned_flags
    assert not fa.errors
    assert "-fgenut-unknown-flag" in learned.read_text()


@pytest.mark.libclang
def test_linkscope_gtest(rb_analysis):
    fn = rb_analysis.get("rb_push")
    rules = Rules(must_stub=["mem*"])
    ctx = GTestLinkContext(product_root=SAMPLE, link_sources=[RB])
    j = {x.name: x for x in judge_calls(fn, RB, "gtest", rules, ctx)}
    assert j["rb_free_space"].scope == Scope.SAME_TU
    assert j["rb_alloc_slot"].scope == Scope.UNDEFINED
    assert j["memcpy"].scope == Scope.MUST_STUB
    ctx2 = GTestLinkContext(product_root=SAMPLE, link_sources=[RB, SAMPLE / "src" / "slot.c"], system_dirs=[SYSINC])
    j2 = {x.name: x for x in judge_calls(fn, RB, "gtest", Rules(), ctx2)}
    assert j2["rb_alloc_slot"].scope == Scope.LINKABLE
    assert j2["memcpy"].scope == Scope.EXTERNAL
    report = AnalysisReport(function=fn, linkscope=list(j.values()))
    assert set(report.stub_required()) == {"rb_alloc_slot", "memcpy"}


@pytest.mark.libclang
def test_linkscope_kernel(tmp_path, rb_analysis):
    fn = rb_analysis.get("rb_push")
    other = tmp_path / "other.c"
    other.write_text("unsigned char *rb_alloc_slot(void *rb, unsigned long len)\n{\n\treturn 0;\n}\nEXPORT_SYMBOL_GPL(rb_alloc_slot);\n")
    ctx = KernelLinkContext(kernel_src=tmp_path, test_kind="module", built_sources=[other], cache_path=tmp_path / "cache.json")
    j = {x.name: x for x in judge_calls(fn, RB, "kunit", Rules(), ctx)}
    assert j["rb_alloc_slot"].scope == Scope.LINKABLE and "EXPORT" in j["rb_alloc_slot"].reason
    assert json.loads((tmp_path / "cache.json").read_text())["exports"] == ["rb_alloc_slot"]
    ctx2 = KernelLinkContext(kernel_src=tmp_path, test_kind="module", built_sources=[])
    j2 = {x.name: x for x in judge_calls(fn, RB, "kunit", Rules(), ctx2)}
    assert j2["rb_alloc_slot"].scope == Scope.UNDEFINED


@pytest.mark.libclang
def test_summarizer(rb_analysis):
    fn = rb_analysis.get("rb_push")
    ctx = GTestLinkContext(product_root=SAMPLE, link_sources=[RB])
    r = AnalysisReport(function=fn, linkscope=judge_calls(fn, RB, "gtest", Rules(), ctx))
    src = numbered_source(fn)
    assert src.splitlines()[0].startswith(f"{fn.start_line}| int rb_push")
    frags = {f.name: f for f in report_fragments(r)}
    assert "rb_alloc_slot  undefined → 스텁 필요" in frags["analysis.calls"].text
    assert frags["analysis.types"].priority == 2 and frags["analysis.target"].priority == 1
    shrunk = frags["analysis.types"].shrinkers[1]()
    assert "struct rb (struct)" in shrunk
    md = render_report(r)
    assert "## 스텁 필수 심볼" in md and "`rb_alloc_slot`" in md
