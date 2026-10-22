"""CMake 실행기 (설계 7.5): GenUT 전용 빌드 디렉터리, 관리 CMakeLists, CMake File API.

GenUT 빌드는 사용자의 최상위 CMakeLists.txt를 그대로 쓴다. 최상위 project() 직후 실행되는 주입 파일
(CMAKE_PROJECT_INCLUDE)이 최상위 처리가 끝날 때 작업 디렉터리(빌드 디렉터리 안 genut-src/)의
CMakeLists.txt를 include 한다. 작업 중 테스트 파일과 관리 CMakeLists는 빌드 디렉터리 안에만 있으므로
프로덕트 소스 트리는 최종 설치 전까지 건드리지 않는다(11.2).

관리 CMakeLists는 테스트 파일을 glob으로 찾아 파일마다 실행 파일 타깃 genut_<src>_<func>를 만든다.
설치 때 같은 템플릿을 cmake_lists_dir에 쓰며, 사용자가 루트에 add_subdirectory를 한 줄 넣으면 사용자
빌드에서도 같은 테스트가 빌드된다. GenUT 빌드(GENUT_WORK_DIR 정의)에서는 설치된 쪽을 건너뛴다.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from genut.backends.base import CommandResult, Runner, run_command
from genut.config import GenutConfig

INJECT = """\
# GenUT 주입 파일 (자동 생성). 최상위 project() 직후 실행되고, 최상위 처리가 끝날 때 작업 디렉터리의
# CMakeLists.txt를 include 한다. 그때는 사용자 타깃과 디렉터리 설정이 모두 정의되어 있다.
# (지연 실행에서는 add_subdirectory를 쓸 수 없다)
if(NOT COMMAND genut_add_work_tests)
  enable_language(CXX)
  function(genut_add_work_tests)
    include("${GENUT_WORK_DIR}/CMakeLists.txt")
  endfunction()
  cmake_language(DEFER DIRECTORY "${CMAKE_SOURCE_DIR}" CALL genut_add_work_tests)
endif()
"""

MANAGED = """\
# GenUT가 관리하는 파일입니다 (설계 7.5). 직접 고치면 다음 설치 때 덮어씁니다.
# 루트 CMakeLists.txt에 add_subdirectory(<이 디렉터리>)를 한 줄 넣으면 사용자 빌드에서도 테스트를 빌드합니다.
cmake_minimum_required(VERSION 3.19)
if(DEFINED GENUT_WORK_DIR)
  file(REAL_PATH "${GENUT_WORK_DIR}" _genut_work)
  file(REAL_PATH "${CMAKE_CURRENT_LIST_DIR}" _genut_here)
  if(NOT _genut_here STREQUAL _genut_work)
    return()  # GenUT 전용 빌드에서는 작업 중 파일만 빌드한다
  endif()
endif()
if(NOT CMAKE_CXX_COMPILER_LOADED)
  enable_language(CXX)
endif()
@GTEST_ROOT@find_package(GTest REQUIRED)
# 테스트 파일별 링크 타깃: set(GENUT_LINKS_<이름> ...). 없으면 아래 기본값
include("${CMAKE_CURRENT_LIST_DIR}/genut_links.cmake" OPTIONAL)
file(GLOB _genut_tests CONFIGURE_DEPENDS "${CMAKE_CURRENT_LIST_DIR}/@TEST_GLOB@")
foreach(_genut_src IN LISTS _genut_tests)
  get_filename_component(_genut_stem "${_genut_src}" NAME_WE)
  string(REGEX REPLACE "_test$" "" _genut_name "${_genut_stem}")
  if(DEFINED GENUT_LINKS_${_genut_name})
    set(_genut_links ${GENUT_LINKS_${_genut_name}})
  else()
    set(_genut_links @LINK_TARGETS@)
  endif()
  add_executable(genut_${_genut_name} "${_genut_src}")
  target_link_libraries(genut_${_genut_name} PRIVATE ${_genut_links} GTest::gtest_main)
endforeach()
"""

_QUERY = Path(".cmake") / "api" / "v1"
_LINKS = re.compile(r"^set\(GENUT_LINKS_(\w+)((?:\s+[^\s)]+)*)\s*\)\s*$", re.M)


def file_glob(pattern: str) -> str:
    """test_file_pattern("{src}_{func}_test.cc")을 glob("*_*_test.cc")으로."""
    return re.sub(r"\{\w+\}", "*", pattern)


def unit_key(file_name: str) -> str:
    """테스트 파일 이름에서 타깃 이름의 몸통 (ring_buffer_rb_push_test.cc → ring_buffer_rb_push)."""
    return re.sub(r"_test$", "", file_name.split(".", 1)[0])


def target_name(file_name: str) -> str:
    """테스트 파일 이름에서 CMake 타깃 이름. 관리 CMakeLists와 같은 규칙."""
    return "genut_" + unit_key(file_name)


def managed_cmakelists(link_targets: list[str], pattern: str, test_dir: str = ".", gtest_root: str = "") -> str:
    rel = test_dir.replace("\\", "/").strip("/")
    glob = file_glob(pattern) if rel in ("", ".") else f"{rel}/{file_glob(pattern)}"
    root = f'set(GTest_ROOT "{Path(gtest_root).as_posix()}")\n' if gtest_root else ""
    return MANAGED.replace("@GTEST_ROOT@", root).replace("@TEST_GLOB@", glob).replace("@LINK_TARGETS@", " ".join(link_targets))


def parse_links(text: str) -> dict[str, list[str]]:
    return {m.group(1): m.group(2).split() for m in _LINKS.finditer(text)}


def render_links(links: dict[str, list[str]]) -> str:
    lines = ["# GenUT가 관리하는 파일입니다. 테스트 파일별 링크 타깃 (설계 7.5)."]
    lines += [f"set(GENUT_LINKS_{k} {' '.join(v)})" for k, v in sorted(links.items()) if v]
    return "\n".join(lines) + "\n"


def compile_flags(cfg: GenutConfig) -> tuple[list[str], list[str]]:
    """전용 빌드에 넣을 (컴파일, 링크) 플래그: 최적화 수준, 커버리지 계측, 새니타이저 (설계 7.5, 7.6).

    새니타이저는 첫 리포트에서 멈추게 한다(-fno-sanitize-recover). UBSan은 기본으로 계속 실행해
    정의되지 않은 동작을 한 케이스가 통과로 끝날 수 있기 때문이다. 멈추면 크래시 경로로 잡힌다.
    """
    g, comp, link = cfg.gtest, [], []
    if g.coverage_opt_level:
        comp += [f"-{g.coverage_opt_level}", "-g"]
    if cfg.coverage.enabled:
        if cfg.coverage.tool == "gcov":
            comp.append("--coverage")
            link.append("--coverage")
        else:
            comp += ["-fprofile-instr-generate", "-fcoverage-mapping"]
            link.append("-fprofile-instr-generate")
    if g.sanitizers:
        s = ",".join(g.sanitizers)
        comp += [f"-fsanitize={s}", f"-fno-sanitize-recover={s}", "-fno-omit-frame-pointer"]
        link.append(f"-fsanitize={s}")
    return comp, link


# ------------------------------------------------------------- File API
@dataclass
class CMakeTarget:
    name: str
    type: str  # EXECUTABLE | STATIC_LIBRARY | ...
    sources: list[Path] = field(default_factory=list)
    artifacts: list[Path] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)  # 타깃 이름


def write_query(build_dir: Path) -> None:
    q = build_dir / _QUERY / "query" / "codemodel-v2"
    q.parent.mkdir(parents=True, exist_ok=True)
    q.touch()


def read_codemodel(build_dir: Path) -> dict[str, CMakeTarget]:
    """File API 응답(codemodel-v2)에서 타깃과 소스·산출물·의존 타깃을 읽는다. 응답이 없으면 빈 dict."""
    reply = build_dir / _QUERY / "reply"
    indexes = sorted(reply.glob("index-*.json")) if reply.is_dir() else []
    if not indexes:
        return {}
    index = json.loads(indexes[-1].read_text(encoding="utf-8"))
    obj = next((o for o in index.get("objects", []) if o.get("kind") == "codemodel"), None)
    if obj is None:
        return {}
    cm = json.loads((reply / obj["jsonFile"]).read_text(encoding="utf-8"))
    src_root = Path(cm["paths"]["source"])
    configs = cm.get("configurations") or [{}]
    ids: dict[str, str] = {}
    raw: list[dict] = []
    for t in configs[0].get("targets", []):
        data = json.loads((reply / t["jsonFile"]).read_text(encoding="utf-8"))
        ids[data["id"]] = data["name"]
        raw.append(data)
    out: dict[str, CMakeTarget] = {}
    for data in raw:
        out[data["name"]] = CMakeTarget(
            name=data["name"],
            type=data.get("type", ""),
            sources=[(src_root / s["path"]).resolve() for s in data.get("sources", [])],
            artifacts=[(build_dir / a["path"]).resolve() if not Path(a["path"]).is_absolute() else Path(a["path"]) for a in data.get("artifacts", [])],
            dependencies=[ids[d["id"]] for d in data.get("dependencies", []) if d.get("id") in ids],
        )
    return out


def link_closure(targets: dict[str, CMakeTarget], names: list[str]) -> list[CMakeTarget]:
    """names와 그 의존 타깃 전부."""
    seen: list[str] = []
    stack = list(names)
    while stack:
        n = stack.pop()
        if n in seen or n not in targets:
            continue
        seen.append(n)
        stack += targets[n].dependencies
    return [targets[n] for n in seen]


def suggest_link_targets(targets: dict[str, CMakeTarget], source: Path) -> list[str]:
    """대상 소스를 담은 라이브러리 타깃 (link_targets가 비었을 때 제안)."""
    want = os.path.normcase(str(source.resolve()))
    libs = ("STATIC_LIBRARY", "SHARED_LIBRARY", "OBJECT_LIBRARY", "MODULE_LIBRARY")
    return sorted(
        t.name for t in targets.values()
        if t.type in libs and not t.name.startswith("genut_") and any(os.path.normcase(str(s)) == want for s in t.sources)
    )


# ---------------------------------------------------------------- CMake
class CMake:
    def __init__(self, cfg: GenutConfig, source_dir: Path, build_dir: Path, runner: Runner = run_command) -> None:
        self.cfg = cfg
        self.source_dir = source_dir
        self.build_dir = build_dir
        self.work_dir = build_dir / "genut-src"
        self.inject_file = build_dir / "genut-inject.cmake"
        self.runner = runner
        self.exe = cfg.gtest.cmake_exe

    def generator(self) -> str:
        cache = self.build_dir / "CMakeCache.txt"
        if cache.exists():
            m = re.search(r"^CMAKE_GENERATOR:INTERNAL=(.*)$", cache.read_text(encoding="utf-8", errors="replace"), re.M)
            return m.group(1).strip() if m else ""
        return ""

    def write_work_lists(self, per_test: dict[str, list[str]]) -> None:
        """작업 디렉터리의 관리 CMakeLists와 테스트별 링크 타깃, 주입 파일을 쓴다 (바뀐 것만)."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        g = self.cfg.gtest
        for p, text in (
            (self.work_dir / "CMakeLists.txt", managed_cmakelists(g.link_targets, g.test_file_pattern, ".", g.gtest_root)),
            (self.work_dir / "genut_links.cmake", render_links(per_test)),
            (self.inject_file, INJECT),
        ):
            if not p.exists() or p.read_text(encoding="utf-8") != text:
                p.write_text(text, encoding="utf-8")

    def configure(self, timeout: float | None = None) -> CommandResult:
        self.build_dir.mkdir(parents=True, exist_ok=True)
        write_query(self.build_dir)
        comp, link = compile_flags(self.cfg)
        cmd = [self.exe, "-S", str(self.source_dir), "-B", str(self.build_dir)]
        if not self.generator() and shutil.which("ninja"):
            cmd += ["-G", "Ninja"]
        cmd += [
            f"-DCMAKE_PROJECT_INCLUDE={self.inject_file.as_posix()}",
            f"-DGENUT_WORK_DIR={self.work_dir.resolve().as_posix()}",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            f"-DCMAKE_C_FLAGS={' '.join(comp)}",
            f"-DCMAKE_CXX_FLAGS={' '.join(comp)}",
            f"-DCMAKE_EXE_LINKER_FLAGS={' '.join(link)}",
            *self.cfg.gtest.cmake_configure_args,
        ]
        return self.runner(cmd, timeout=timeout)

    def build(self, targets: list[str], timeout: float | None = None) -> CommandResult:
        cmd = [self.exe, "--build", str(self.build_dir), "--parallel", "--target", *targets]
        gen = self.generator()
        if "Ninja" in gen:
            cmd += ["--", "-k", "0"]  # 한 타깃이 실패해도 나머지는 빌드한다
        elif "Makefiles" in gen:
            cmd += ["--", "-k"]
        return self.runner(cmd, timeout=timeout)

    def codemodel(self) -> dict[str, CMakeTarget]:
        return read_codemodel(self.build_dir)
