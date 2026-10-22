import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from conftest import FIXTURES, SYSINC
from genut.analysis import compile_db
from genut.analysis.compile_db import CompileEntry, builtin_include_args, compiler_search_dirs, compiler_target
from genut.analysis.model import AnalysisReport, FunctionInfo, GlobalRef, Scope, SymbolJudgment
from genut.backends.base import CommandResult, TestUnit
from genut.backends.gtest import backend as gtest_backend
from genut.backends.gtest import skeleton
from genut.backends.gtest.backend import GTestBackend
from genut.backends.gtest.cmake_runner import (
    INJECT,
    compile_flags,
    link_closure,
    managed_cmakelists,
    parse_links,
    read_codemodel,
    render_links,
    suggest_link_targets,
    target_name,
    file_glob,
)
from genut.backends.stub import NoopStubAdapter
from genut.config import GenutConfig
from genut.state import CaseStatus, FunctionState, SharedArea, TestCase
from genut.verify.logs.crash import CrashContext, build_brief
from genut.verify.staged import semantic_check

EXE = ".exe" if os.name == "nt" else ""
LOGS = FIXTURES / "logs"


def _unit(source="src/ring_buffer.c", name="rb_push", cases=(), **fn):
    info = dict(name=name, signature=f"int {name}(void)", return_type="int", file=source, start_line=1, end_line=2, includes=['#include "rb.h"'])
    info.update(fn)
    state = FunctionState(name=name, cases=[TestCase(name=n, target_function=name, body=b) for n, b in cases])
    return TestUnit(source=Path("/proj") / source, report=AnalysisReport(function=FunctionInfo(**info)), state=state)


# ------------------------------------------------------------------ skeleton
def test_suite_name():
    assert skeleton.suite_name(Path("src/ring_buffer.c"), "rb_push") == "RingBufferRbPushTest"
    assert skeleton.suite_name(Path("9lives.c"), "f") == "T9livesFTest"


def test_render_c_source_wraps_includes_and_maps_cases():
    u = _unit(cases=[("test_rb_push_ok", "EXPECT_EQ(rb_push(), 0);"), ("test_rb_push_two", "int x = 1;\n\nEXPECT_EQ(rb_push(), x);")])
    u.state.cases[1].status = CaseStatus.DROPPED  # 살아 있는 케이스만 들어간다
    r = skeleton.render(u, "ring_buffer_rb_push_test.cc")
    lines = r.text.splitlines()
    assert lines[1] == "#include <gtest/gtest.h>" and 'extern "C" {' in lines and '#include "rb.h"' in lines
    assert r.cases == ["test_rb_push_ok"] and r.suite == "RingBufferRbPushTest"
    start, end = r.line_map.cases["test_rb_push_ok"]
    assert lines[start - 1] == "TEST_F(RingBufferRbPushTest, test_rb_push_ok) {" and lines[end - 1] == "}"
    assert lines[start] == "  EXPECT_EQ(rb_push(), 0);"
    assert r.line_map.function_names == {"RingBufferRbPushTest_test_rb_push_ok_Test": "test_rb_push_ok"}
    assert r.line_map.owner_of(start + 1) == "test_rb_push_ok" and r.line_map.owner_of(2) == "@shared"


def test_render_globals_helpers_and_cpp_source():
    g = [GlobalRef(name="g_count", access="write", decl_file="/proj/include/rb.h"),
         GlobalRef(name="stats", access="write", is_static=True, decl_file="/proj/src/ring_buffer.c"),
         GlobalRef(name="g_ro", access="read", decl_file="/proj/include/rb.h")]
    u = _unit(cases=[("test_a", "fill(&rb);\nEXPECT_EQ(rb_push(), 0);")], globals=g)
    u.state.shared = SharedArea(helpers={"make": "static void make(void) {}", "fill": "static void fill(struct rb *r) { make(); }", "unused": "static void unused(void) {}"},
                                setup_code="setup_hook();", teardown_code="teardown_hook();")
    text = skeleton.render(u, "x_test.cc").text
    assert "static unsigned char genut_saved_g_count[sizeof(g_count)];" in text and "genut_saved_stats" not in text and "g_ro" not in text
    assert text.index("std::memcpy(genut_saved_g_count") < text.index("    setup_hook();")
    assert text.index("    teardown_hook();") < text.index("std::memcpy(&g_count")
    assert "static void make" in text and "static void fill" in text and "unused" not in text
    assert text.index("static void make") < text.index("static void fill")  # 공유 영역 순서 유지

    cpp = _unit(source="src/list.cpp", name="push", cases=[("test_push", "EXPECT_TRUE(push());")])
    t = skeleton.render(cpp, "list_push_test.cc").text
    assert 'extern "C"' not in t and '#include "rb.h"' in t


def test_render_empty_skeleton_for_preflight():
    u = _unit(cases=[("test_a", "helper();")])
    u.state.shared = SharedArea(helpers={"helper": "static void helper(void) {}"})
    r = skeleton.render(u, "x_test.cc", [])
    assert r.cases == [] and "TEST_F" not in r.text and "helper" not in r.text and "class RingBufferRbPushTest" in r.text


# ------------------------------------------------------------------- cmake
def test_names_globs_and_links():
    assert file_glob("{src}_{func}_test.cc") == "*_*_test.cc"
    assert target_name("ring_buffer_rb_push_test.cc") == "genut_ring_buffer_rb_push"
    links = {"ring_buffer_rb_push": ["rb", "rbslot"], "str_util_str_clamp": ["strutil"], "empty": []}
    assert parse_links(render_links(links)) == {k: v for k, v in links.items() if v}


def test_managed_cmakelists():
    work = managed_cmakelists(["rb"], "{src}_{func}_test.cc")
    assert '"${CMAKE_CURRENT_LIST_DIR}/*_*_test.cc"' in work and "set(_genut_links rb)" in work and "GTest_ROOT" not in work
    inst = managed_cmakelists([], "{src}_{func}_test.cc", "../generated", "/opt/gtest")
    assert '"${CMAKE_CURRENT_LIST_DIR}/../generated/*_*_test.cc"' in inst and 'set(GTest_ROOT "/opt/gtest")' in inst
    assert "GENUT_WORK_DIR" in inst and "genut_links.cmake" in inst
    assert "cmake_language(DEFER" in INJECT and "include(" in INJECT and "add_subdirectory" not in INJECT.split("(지연")[0]


def test_compile_flags():
    cfg = GenutConfig()
    assert compile_flags(cfg) == (["-O0", "-g", "--coverage"], ["--coverage"])
    cfg = GenutConfig.model_validate({"coverage": {"tool": "llvm-cov"}, "gtest": {"sanitizers": ["address", "undefined"], "coverage_opt_level": ""}})
    comp, link = compile_flags(cfg)
    assert comp == ["-fprofile-instr-generate", "-fcoverage-mapping", "-fsanitize=address,undefined",
                    "-fno-sanitize-recover=address,undefined", "-fno-omit-frame-pointer"]
    assert link == ["-fprofile-instr-generate", "-fsanitize=address,undefined"]
    assert compile_flags(GenutConfig.model_validate({"coverage": {"enabled": False}}))[1] == []


def _codemodel(build, source):
    reply = build / ".cmake" / "api" / "v1" / "reply"
    reply.mkdir(parents=True)
    (reply / "index-2026-10-22.json").write_text(json.dumps({"objects": [{"kind": "codemodel", "jsonFile": "codemodel-v2.json"}]}))
    targets = [
        {"name": "rb", "id": "rb::@1", "type": "STATIC_LIBRARY", "sources": [{"path": "src/ring_buffer.c"}], "artifacts": [{"path": "librb.a"}], "dependencies": [{"id": "rbslot::@1"}]},
        {"name": "rbslot", "id": "rbslot::@1", "type": "STATIC_LIBRARY", "sources": [{"path": "src/slot.c"}], "artifacts": [{"path": "librbslot.a"}]},
        {"name": "genut_ring_buffer_rb_push", "id": "g::@1", "type": "EXECUTABLE", "sources": [{"path": "src/ring_buffer.c"}], "artifacts": [{"path": "bin/genut_ring_buffer_rb_push"}]},
    ]
    cm = {"paths": {"source": str(source), "build": str(build)}, "configurations": [{"targets": [{"name": t["name"], "jsonFile": f"t-{t['name']}.json"} for t in targets]}]}
    (reply / "codemodel-v2.json").write_text(json.dumps(cm))
    for t in targets:
        (reply / f"t-{t['name']}.json").write_text(json.dumps(t))


def test_read_codemodel_closure_and_suggestion(tmp_path):
    src, build = tmp_path / "proj", tmp_path / "proj" / "build"
    assert read_codemodel(build) == {}
    _codemodel(build, src)
    t = read_codemodel(build)
    assert t["rb"].sources == [(src / "src" / "ring_buffer.c").resolve()] and t["rb"].dependencies == ["rbslot"]
    assert t["genut_ring_buffer_rb_push"].artifacts == [(build / "bin" / "genut_ring_buffer_rb_push").resolve()]
    assert [x.name for x in link_closure(t, ["rb"])] == ["rb", "rbslot"]
    assert suggest_link_targets(t, src / "src" / "ring_buffer.c") == ["rb"]  # 실행 파일 타깃은 제외


# ---------------------------------------------------------- fake toolchain
class FakeRunner:
    """cmake와 테스트 실행 파일 대신 응답한다. 핸들러는 (cmd, cwd) -> CommandResult | None."""

    def __init__(self, build_dir: Path):
        self.build_dir = build_dir
        self.calls: list[list[str]] = []
        self.configure_rc = 0
        self.build_output = ""
        self.built: set[str] = set()
        self.exe_handler = None
        self.gdb_output = ""

    def __call__(self, cmd, cwd=None, timeout=None, env=None):
        self.calls.append(list(cmd))
        if cmd[1:2] == ["-S"]:
            (self.build_dir / "CMakeCache.txt").write_text("CMAKE_GENERATOR:INTERNAL=Ninja\n")
            return CommandResult(cmd, self.configure_rc, "-- Configuring done\n" if not self.configure_rc else "CMake Error: GTest not found\n", 0.1)
        if cmd[1:2] == ["--build"]:
            targets = cmd[cmd.index("--target") + 1 : cmd.index("--")] if "--" in cmd else cmd[cmd.index("--target") + 1 :]
            for t in targets:
                if t in self.built:
                    (self.build_dir / (t + EXE)).write_text("exe")
            return CommandResult(cmd, 0 if set(targets) <= self.built else 1, self.build_output, 0.2)
        if "gdb" in Path(cmd[0]).name:
            return CommandResult(cmd, 0, self.gdb_output, 0.1)
        return self.exe_handler(cmd, cwd)


@pytest.fixture
def backend(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    cfg = GenutConfig.model_validate({"gtest": {"link_targets": ["rb"], "test_timeout_sec": 5}})
    fake = FakeRunner(root / "build-genut")
    return GTestBackend(cfg, root, runner=fake), fake


def test_configure_and_build_commands(backend):
    be, fake = backend
    u = _unit(cases=[("test_a", "EXPECT_EQ(1, 1);")])
    fake.built = {"genut_ring_buffer_rb_push"}
    assert be.preflight([u]).ok
    conf, build = fake.calls[0], fake.calls[1]
    assert conf[:5] == ["cmake", "-S", str(be.root), "-B", str(be.build_dir)]
    assert f"-DCMAKE_PROJECT_INCLUDE={(be.build_dir / 'genut-inject.cmake').as_posix()}" in conf
    assert f"-DGENUT_WORK_DIR={be.work_dir.resolve().as_posix()}" in conf and "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON" in conf
    assert build[-4:] == ["genut_ring_buffer_rb_push", "--", "-k", "0"]  # Ninja면 실패해도 나머지 타깃을 빌드
    assert (be.work_dir / "CMakeLists.txt").exists() and (be.work_dir / "ring_buffer_rb_push_test.cc").exists()
    assert not list(be.root.glob("tests"))  # 작업 중에는 프로덕트 트리에 쓰지 않는다


def test_preflight_failures(backend):
    be, fake = backend
    u = _unit()
    fake.configure_rc = 1
    pf = be.preflight([u])
    assert not pf.ok and pf.category == "infra_error" and "GTest not found" in pf.log
    fake.configure_rc = 0
    fake.build_output = "/proj/include/rb.h:12:5: error: expected unqualified-id before 'new'\n"
    pf = be.preflight([u])
    assert not pf.ok and pf.category == "header_not_cpp_clean" and pf.diagnostics[0].file == "/proj/include/rb.h"
    fake.built = {"genut_ring_buffer_rb_push"}
    assert be.preflight([u]).ok


def test_build_attributes_diagnostics(backend, tmp_path):
    be, fake = backend
    u1 = _unit(cases=[("test_rb_push_ok", "EXPECT_EQ(rb_push(), 0);"), ("test_rb_push_typo", "EXPECT_EQ(rb_psh(), 0);")])
    u2 = _unit(source="src/str_util.c", name="str_clamp", cases=[("test_str_clamp_low", "EXPECT_EQ(str_clamp(), 0);")])
    r1, r2 = be.render(u1), be.render(u2)
    typo_line = r1.line_map.cases["test_rb_push_typo"][0] + 1
    p1 = (be.work_dir / r1.file_name).as_posix()
    fake.built = {"genut_ring_buffer_rb_push"}
    fake.build_output = (
        "[1/4] Building CXX object CMakeFiles/genut_str_util_str_clamp.dir/genut-src/str_util_str_clamp_test.cc.obj\n"
        "[2/4] Linking CXX executable genut_str_util_str_clamp\n"
        "FAILED: genut_str_util_str_clamp\n"
        "ld: CMakeFiles/genut_str_util_str_clamp.dir/genut-src/str_util_str_clamp_test.cc.obj: in function `StrUtilStrClampTest_test_str_clamp_low_Test::TestBody()':\n"
        "str_util_str_clamp_test.cc:15:(.text+0x39): undefined reference to `str_clamp'\n"
        "[3/4] Building CXX object CMakeFiles/genut_ring_buffer_rb_push.dir/genut-src/ring_buffer_rb_push_test.cc.obj\n"
        f"{p1}:{typo_line}:13: warning: unused variable 'x' [-Wunused-variable]\n"
        "ninja: build stopped: subcommand failed.\n"
    )
    out = be.build([r1, r2], tmp_path / "out")
    assert out.ok_functions == ["rb_push"] and out.failed_functions == ["str_clamp"]
    link = out.diagnostics["str_clamp"][0]
    assert link.stage == "link" and link.owner == "test_str_clamp_low"
    assert [(d.owner, d.severity) for d in out.diagnostics["rb_push"]] == [("test_rb_push_typo", "warning")]
    assert (tmp_path / "out" / "build.log").read_text(encoding="utf-8") .count("FAILED") == 1


def test_build_header_error_goes_to_shared(backend):
    be, fake = backend
    r = be.render(_unit(cases=[("test_a", "EXPECT_EQ(1, 1);")]))
    fake.build_output = ("FAILED: CMakeFiles/genut_ring_buffer_rb_push.dir/genut-src/ring_buffer_rb_push_test.cc.obj\n"
                         "/proj/include/rb.h:3:1: error: unknown type name 'u8'\n")
    out = be.build([r])
    assert out.failed_functions == ["rb_push"] and out.diagnostics["rb_push"][0].owner == "@shared"


def _gtest_json(path, suite, cases):
    ts = [{"name": n, "status": "RUN", "result": "COMPLETED", **({"failures": [{"failure": f"t.cc:9\n{fail}"}]} if fail else {})} for n, fail in cases]
    Path(path).write_text(json.dumps({"testsuites": [{"name": suite, "testsuite": ts}]}), encoding="utf-8")


def test_run_parses_json_and_isolates(backend, tmp_path):
    be, fake = backend
    r = be.render(_unit(cases=[("test_ok", "EXPECT_EQ(1, 1);"), ("test_bad", "EXPECT_EQ(1, 2);"), ("test_more", "EXPECT_EQ(2, 2);")]))
    seen = {}

    def exe(cmd, cwd):
        seen["cmd"], seen["cwd"] = cmd, cwd
        out = next(a.split(":", 1)[1] for a in cmd if a.startswith("--gtest_output=json:"))
        _gtest_json(out, r.suite, [("test_ok", ""), ("test_bad", "Expected equality\n  Which is: 2")])
        return CommandResult(cmd, 1, "[ RUN      ] x\n", 0.1)

    fake.exe_handler = exe
    rr = be.run(r, tmp_path / "out", only=["test_ok", "test_bad"])
    assert seen["cmd"][0] == str(be.build_dir / ("genut_ring_buffer_rb_push" + EXE))
    assert seen["cmd"][-1] == "--gtest_filter=RingBufferRbPushTest.test_ok:RingBufferRbPushTest.test_bad"
    assert Path(seen["cwd"]) == tmp_path / "out" / "run-rb_push"
    assert {k: v.status for k, v in rr.results.items()} == {"test_ok": "pass", "test_bad": "fail"}
    assert rr.results["test_bad"].observed == ["2"] and rr.crash is None


def test_run_crash_uses_gdb_backtrace(backend, tmp_path, monkeypatch):
    be, fake = backend
    r = be.render(_unit(cases=[("test_ok", "a();"), ("test_rb_push_bad_buf", "b();"), ("test_after", "c();")]))
    stdout = ("[ RUN      ] RingBufferRbPushTest.test_ok\n[       OK ] RingBufferRbPushTest.test_ok (0 ms)\n"
              "[ RUN      ] RingBufferRbPushTest.test_rb_push_bad_buf\n")
    fake.exe_handler = lambda cmd, cwd: CommandResult(cmd, 0xC0000005 if os.name == "nt" else -11, stdout, 0.1)
    fake.gdb_output = (LOGS / "gdb_segv.log").read_text(encoding="utf-8")
    monkeypatch.setattr(gtest_backend.shutil, "which", lambda name: "/usr/bin/gdb" if name == "gdb" else None)
    rr = be.run(r, tmp_path / "out")
    assert {k: v.status for k, v in rr.results.items()} == {"test_ok": "pass", "test_rb_push_bad_buf": "crash", "test_after": "not_run"}
    assert rr.crash.category == "segv" and rr.crash.suspect_case == "test_rb_push_bad_buf"
    assert rr.crash.location == "/work/proj/src/ring_buffer.c:39"
    gdb_call = next(c for c in fake.calls if "gdb" in Path(c[0]).name)
    assert gdb_call[-2] == "--gtest_filter=RingBufferRbPushTest.test_rb_push_bad_buf"
    assert "gdb 백트레이스" in (tmp_path / "out" / "rb_push.run.log").read_text(encoding="utf-8")


def test_run_crash_without_gdb_uses_exit_code(backend, tmp_path):
    be, fake = backend
    be.cfg.gtest.crash_backtrace = ""
    r = be.render(_unit(cases=[("test_a", "a();"), ("test_b", "b();")]))
    fake.exe_handler = lambda cmd, cwd: CommandResult(cmd, -11, "[ RUN      ] RingBufferRbPushTest.test_a\n", 0.1)
    rr = be.run(r, tmp_path / "out")
    assert rr.crash.category == "segv" and rr.crash.suspect_case == "test_a" and rr.crash.raw_excerpt == "종료 코드 -11"
    assert rr.results["test_a"].status == "crash" and rr.results["test_b"].status == "not_run"
    assert not [c for c in fake.calls if "gdb" in Path(c[0]).name]


def test_run_timeout(backend, tmp_path):
    be, fake = backend
    r = be.render(_unit(cases=[("test_loop", "for (;;) {}"), ("test_b", "b();")]))
    fake.exe_handler = lambda cmd, cwd: CommandResult(cmd, -1, "[ RUN      ] RingBufferRbPushTest.test_loop\n", 5.0, timed_out=True)
    rr = be.run(r)
    assert rr.timed_out and rr.results["test_loop"].status == "timeout" and rr.results["test_b"].status == "not_run"
    assert rr.crash.category == "hang_timeout" and rr.crash.suspect_case == "test_loop"


def test_install_keeps_other_functions(backend):
    be, _ = backend
    be.links = {"ring_buffer_rb_push": ["rb", "rbslot"]}
    g = be.root / "tests" / "generated"
    g.mkdir(parents=True)
    (g / "str_util_str_clamp_test.cc").write_text("// 이전 실행의 파일\n", encoding="utf-8")
    (g / "genut_links.cmake").write_text(render_links({"str_util_str_clamp": ["strutil"]}), encoding="utf-8")
    r = be.render(_unit(cases=[("test_a", "EXPECT_EQ(1, 1);")]))
    path = be.install(r)
    assert path == g / "ring_buffer_rb_push_test.cc" and path.read_text(encoding="utf-8") == r.text
    assert (g / "str_util_str_clamp_test.cc").read_text(encoding="utf-8") == "// 이전 실행의 파일\n"
    assert parse_links((g / "genut_links.cmake").read_text(encoding="utf-8")) == {"str_util_str_clamp": ["strutil"], "ring_buffer_rb_push": ["rb", "rbslot"]}
    lists = (g / "CMakeLists.txt").read_text(encoding="utf-8")
    assert '"${CMAKE_CURRENT_LIST_DIR}/*_*_test.cc"' in lists and "set(_genut_links rb)" in lists


# ---------------------------------------------------------------- noop stub
def test_noop_stub_adapter():
    fn = FunctionInfo(name="rb_push", signature="", return_type="int", file="x.c", start_line=1, end_line=1, is_static=True)
    deps = [SymbolJudgment(name="rb_alloc_slot", scope=Scope.UNDEFINED), SymbolJudgment(name="readl", scope=Scope.MUST_STUB),
            SymbolJudgment(name="memcpy", scope=Scope.EXTERNAL)]
    a = NoopStubAdapter()
    plan = a.plan_stubs(fn, deps, ["readl", "never_called"])
    assert plan.symbols == [] and plan.unsupported == ["rb_alloc_slot", "readl"]
    assert any("static" in n for n in plan.notes)
    assert a.generate(plan, Path(".")).files == [] and "스텁 도구가 아직 연동되지 않았습니다" in a.describe()


# ------------------------------------------------------- sanitizer fixtures
def _brief(name, **ctx):
    return build_brief((LOGS / name).read_text(encoding="utf-8"), CrashContext(**ctx), user=True, suspect_case="x")


def test_asan_heap_use_after_free():
    b = _brief("asan_heap_uaf.log", target_functions={"rb_push"}, test_functions={"RingBufferRbPushTest_test_rb_push_after_free_Test::TestBody"})
    assert b.category == "asan_uaf" and b.location == "/work/proj/src/ring_buffer.c:39"
    assert b.chain[-1].startswith("rb_push") and "RingBufferRbPushTest_test_rb_push_after_free_Test::TestBody" in b.chain[0]
    assert "해제된 메모리" in b.hypothesis


def test_asan_stack_overflow_and_ubsan():
    b = _brief("asan_stack_overflow.log", target_functions={"str_count_char"})
    assert b.category == "asan_oob" and b.location == "/work/proj/src/str_util.c:9"
    u = _brief("ubsan_overflow.log", target_functions={"str_clamp"})
    assert u.category == "ubsan" and u.location == "/work/proj/src/str_util.c:31"


def test_gdb_backtrace_blames_target_frame():
    b = _brief("gdb_segv.log", target_functions={"rb_push"}, test_functions={"RingBufferRbPushTest_test_rb_push_bad_buf_Test::TestBody"})
    assert b.category == "segv" and b.location == "/work/proj/src/ring_buffer.c:39"
    assert [c.split()[0] for c in b.chain] == ["RingBufferRbPushTest_test_rb_push_bad_buf_Test::TestBody", "rb_push"]


# ------------------------------------------------------- compiler headers
def test_compiler_search_dirs_and_target(tmp_path, monkeypatch):
    d1, d2 = tmp_path / "inc1", tmp_path / "inc2"
    d1.mkdir()
    d2.mkdir()
    stderr = f'#include "..." search starts here:\n#include <...> search starts here:\n {d1}\n {d2} (framework directory)\n {tmp_path / "missing"}\nEnd of search list.\n'

    def fake_run(cmd, **kw):
        if "-dumpmachine" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="x86_64-w64-mingw32\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr=stderr)

    monkeypatch.setattr(compile_db.subprocess, "run", fake_run)
    monkeypatch.setattr(compile_db.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(compile_db, "_BUILTIN_CACHE", {})
    monkeypatch.setattr(compile_db, "_MACHINE_CACHE", {})
    assert compiler_search_dirs("g++", "c++") == [os.path.normpath(d1), os.path.normpath(d2)]
    e = CompileEntry(file=Path("t.cc"), directory=tmp_path, arguments=["g++", "-c", "t.cc"], compiler="g++")
    assert builtin_include_args(e) == ["-isystem", os.path.normpath(d1), "-isystem", os.path.normpath(d2)]
    assert compiler_target(e) == "x86_64-w64-mingw32"
    assert builtin_include_args(CompileEntry(file=Path("k.c"), directory=tmp_path, arguments=["gcc", "-nostdinc"], compiler="gcc")) == []


@pytest.mark.libclang
def test_semantic_check_unavailable_when_system_header_breaks(tmp_path):
    sysdir = tmp_path / "sys"
    sysdir.mkdir()
    (sysdir / "broken.h").write_text("struct s { int x }\n", encoding="utf-8")
    r = semantic_check(tmp_path / "t.c", "#include <broken.h>\nint f(void) { return 0; }\n", ["-isystem", str(sysdir), "-isystem", str(SYSINC)])
    assert not r.available and r.ok and "시스템 헤더" in r.reason
    ok = semantic_check(tmp_path / "t.c", "int f(void) { return y; }\n", ["-isystem", str(sysdir)])
    assert ok.available and re.search("undeclared identifier 'y'", ok.diagnostics[0].message)
