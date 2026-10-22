"""gtest 뼈대 (설계 7.1, 7.5, 7.8). 뼈대는 항상 코드가 케이스 목록에서 다시 만든다.

- C 소스를 시험하면 대상 헤더 include를 extern "C" { } 로 감싼다
- 스위트는 함수당 하나. fixture 클래스(밑줄 없는 CamelCase)를 두고 케이스는 TEST_F로 만든다
- 대상 함수가 헤더에 선언된 비static 전역에 쓰면 SetUp에서 저장하고 TearDown에서 되돌린다(케이스별 리셋 훅).
  static 전역은 테스트 파일에서 보이지 않으므로 스텁 도구의 접근 방식이 필요하다
- 공유 헬퍼는 렌더링하는 케이스가 쓰는 것만 넣는다 (안 쓰는 static 함수 경고를 피함)
"""

from __future__ import annotations

import re
from pathlib import Path

from genut.analysis.model import FunctionInfo, GlobalRef
from genut.backends.base import Rendered, TestUnit
from genut.state import TestCase, used_helpers
from genut.verify.diagnostics import LineMap

C_SOURCE_EXT = {".c"}
HEADER_EXT = (".h", ".hh", ".hpp", ".hxx", ".inc")


def suite_name(source: Path, function: str) -> str:
    """ring_buffer + rb_push → RingBufferRbPushTest. 밑줄이 없어 gtest 클래스 이름이 겹치지 않는다."""
    parts = re.split(r"[^A-Za-z0-9]+", f"{source.stem}_{function}")
    name = "".join(p[:1].upper() + p[1:] for p in parts if p)
    return ("T" + name if not name or name[0].isdigit() else name) + "Test"


def resettable_globals(fn: FunctionInfo) -> list[GlobalRef]:
    return [g for g in fn.globals if g.access in ("write", "rw") and not g.is_static and g.decl_file.lower().endswith(HEADER_EXT)]


def _indent(text: str, prefix: str) -> list[str]:
    return [prefix + line if line.strip() else "" for line in text.replace("\r\n", "\n").split("\n")]


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for i in items:
        i = i.strip()
        if i and i not in out:
            out.append(i)
    return out


def render(unit: TestUnit, file_name: str, cases: list[TestCase] | None = None) -> Rendered:
    """cases가 None이면 살아 있는 케이스를 모두 넣는다. 빈 목록이면 사전검증용 빈 뼈대."""
    fn = unit.report.function
    shared = unit.state.shared
    cases = unit.live_cases() if cases is None else cases
    suite = suite_name(unit.source, unit.function)
    globs = resettable_globals(fn)
    lines: list[str] = []

    def add(*items: str) -> None:
        for item in items:
            lines.extend(item.split("\n"))

    add(f"// GenUT가 생성한 테스트입니다: {unit.function} ({unit.source.name}). 직접 고치면 다음 생성 때 덮어씁니다.",
        "#include <gtest/gtest.h>")
    if globs:
        add("#include <cstring>")
    includes = _dedupe(fn.includes + unit.stub.includes + shared.includes)
    if unit.source.suffix in C_SOURCE_EXT:
        add("", 'extern "C" {', *includes, "}")
    elif includes:
        add("", *includes)
    for g in globs:
        add(f"static unsigned char genut_saved_{g.name}[sizeof({g.name})];")
    helpers = used_helpers([c.body for c in cases], shared.helpers)
    for name in shared.helpers:  # 공유 영역에 넣은 순서대로 (헬퍼가 다른 헬퍼를 부를 수 있음)
        if name in helpers:
            add("", shared.helpers[name].strip())

    add("", f"class {suite} : public ::testing::Test {{", " protected:", "  void SetUp() override {")
    for g in globs:
        add(f"    std::memcpy(genut_saved_{g.name}, &{g.name}, sizeof({g.name}));")
    if shared.setup_code.strip():
        lines.extend(_indent(shared.setup_code.strip(), "    "))
    add("  }", "  void TearDown() override {")
    if shared.teardown_code.strip():
        lines.extend(_indent(shared.teardown_code.strip(), "    "))
    for g in globs:
        add(f"    std::memcpy(&{g.name}, genut_saved_{g.name}, sizeof({g.name}));")
    add("  }", "};")

    lm = LineMap(path=file_name)
    for c in cases:
        add("")
        start = len(lines) + 1
        add(f"TEST_F({suite}, {c.name}) {{")
        lines.extend(_indent(c.body.strip("\n"), "  "))
        add("}")
        lm.cases[c.name] = (start, len(lines))
        lm.function_names[f"{suite}_{c.name}_Test"] = c.name
    return Rendered(
        function=unit.function, file_name=file_name, text="\n".join(lines) + "\n", line_map=lm,
        cases=[c.name for c in cases], suite=suite, source=unit.source,
    )
