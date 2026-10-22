"""KUnit 뼈대 (설계 7.1, 7.8). 함수별 테스트 파일과 사용자가 한 번 등록한 집합 파일.

함수별 파일: 대상 헤더 include, 케이스 함수, init/exit(전역 저장·복원과 공유 setup/teardown), kunit_case 테이블,
kunit_suite. 함수별 파일이 자기 include를 갖는다(이전 실행의 다른 소스 파일용 파일도 집합 파일에 함께 들어가므로).
소스 디렉터리 기준인 "foo.h"는 함수별 파일 위치 기준 경로로 바꾼다.
집합 파일: kunit/test.h, 함수별 파일의 #include, 모든 스위트를 담은 kunit_test_suites(...) 한 줄.
함수별 파일들이 집합 파일 안에서 한 번역 단위로 합쳐지므로, 헬퍼는 static과 함수 접두어(<함수>__<헬퍼>)로
이름을 바꾸고 케이스 본문의 호출도 함께 바꾼다. 저장용 전역도 함수 이름을 붙인다.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from genut.backends.base import Rendered, TestUnit
from genut.backends.gtest.skeleton import resettable_globals
from genut.state import TestCase, used_helpers
from genut.verify.diagnostics import LineMap

AGG_HEADER = "/* GenUT가 관리하는 KUnit 집합 파일입니다 (설계 7.8). Makefile/Kconfig 등록은 사용자가 한 번 합니다. 직접 고치면 다음 생성 때 덮어씁니다. */"


def suite_name(source: Path, function: str) -> str:
    return re.sub(r"\W", "_", f"{source.stem}_{function}_test")


def _rename(text: str, mapping: dict[str, str]) -> str:
    for old, new in mapping.items():
        text = re.sub(rf"(?<![\w.>]){re.escape(old)}(?=\s*\()", new, text)
    return text


def _indent(text: str) -> list[str]:
    return ["\t" + line if line.strip() else "" for line in text.replace("\r\n", "\n").strip("\n").split("\n")]


_QUOTE = re.compile(r'^#include\s+"([^"]+)"\s*$')


def relocate(include: str, source_dir: Path, file_dir: Path | None) -> str:
    """소스 디렉터리에 있는 헤더의 따옴표 include를 file_dir 기준 상대 경로로 바꾼다."""
    m = _QUOTE.match(include.strip())
    if file_dir is None or not m or not (source_dir / m.group(1)).exists():
        return include.strip()
    return f'#include "{_rel(source_dir / m.group(1), file_dir)}"'


def render(unit: TestUnit, file_name: str, cases: list[TestCase] | None = None, file_dir: Path | None = None) -> Rendered:
    """file_dir: 함수별 파일이 놓일 디렉터리 (따옴표 include 경로를 맞춘다)."""
    fn, shared, func = unit.report.function, unit.state.shared, unit.function
    cases = unit.live_cases() if cases is None else cases
    suite = suite_name(unit.source, func)
    helpers = used_helpers([c.body for c in cases], shared.helpers)
    mapping = {h: f"{func}__{h}" for h in helpers if not h.startswith(f"{func}__")}
    globs = resettable_globals(fn)
    lines: list[str] = [f"/* GenUT가 생성한 KUnit 테스트입니다: {func} ({unit.source.name}). 집합 파일이 #include 합니다. 직접 고치면 다음 생성 때 덮어씁니다. */"]

    def add(*items: str) -> None:
        for item in items:
            lines.extend(item.split("\n"))

    includes: list[str] = []
    for inc in fn.includes + unit.stub.includes + shared.includes:
        line = relocate(inc, unit.source.parent, file_dir)
        if line and line not in includes:
            includes.append(line)
    add(*includes)
    for g in globs:
        add(f"static unsigned char genut_saved_{func}_{g.name}[sizeof({g.name})];")
    for name in shared.helpers:
        if name in helpers:
            code = _rename(shared.helpers[name].strip(), mapping)
            add("", code if code.lstrip().startswith("static") else "static " + code)
    lm = LineMap(path=file_name)
    for c in cases:
        add("")
        start = len(lines) + 1
        add(f"static void {c.name}(struct kunit *test)", "{")
        lines.extend(_indent(_rename(c.body, mapping)))
        add("}")
        lm.cases[c.name] = (start, len(lines))
        lm.function_names[c.name] = c.name
    add("", f"static int {suite}_init(struct kunit *test)", "{")
    for g in globs:
        add(f"\tmemcpy(genut_saved_{func}_{g.name}, &{g.name}, sizeof({g.name}));")
    if shared.setup_code.strip():
        lines.extend(_indent(shared.setup_code))
    add("\treturn 0;", "}", "", f"static void {suite}_exit(struct kunit *test)", "{")
    if shared.teardown_code.strip():
        lines.extend(_indent(shared.teardown_code))
    for g in globs:
        add(f"\tmemcpy(&{g.name}, genut_saved_{func}_{g.name}, sizeof({g.name}));")
    add("}", "", f"static struct kunit_case {suite}_cases[] = {{")
    add(*[f"\tKUNIT_CASE({c.name})," for c in cases], "\t{}", "};", "")
    add(f"static struct kunit_suite {suite} = {{", f'\t.name = "{suite}",', f"\t.init = {suite}_init,", f"\t.exit = {suite}_exit,",
        f"\t.test_cases = {suite}_cases,", "};")
    return Rendered(function=func, file_name=file_name, text="\n".join(lines) + "\n", line_map=lm, cases=[c.name for c in cases],
                    suite=suite, source=unit.source)


def _rel(path: Path, base: Path) -> str:
    return Path(os.path.relpath(path, base)).as_posix()


PRELUDE = ["#include <kunit/test.h>", "#include <linux/string.h>"]


def aggregate(aggregate_path: Path, files: list[tuple[Path, str]], sources: list[Path] = ()) -> str:
    """files: (함수별 파일 경로, 스위트 이름). sources: static_access = include_source일 때 함께 넣을 대상 소스."""
    base = aggregate_path.parent
    lines = [AGG_HEADER, *PRELUDE]
    lines += [f'#include "{_rel(s, base)}"' for s in sources]
    lines.append("")
    lines += [f'#include "{_rel(p, base)}"' for p, _ in files]
    if files:
        lines += ["", "kunit_test_suites(" + ", ".join(f"&{s}" for _, s in files) + ");"]
    return "\n".join(lines) + "\n"


SUITE_DEF = re.compile(r"^static struct kunit_suite (\w+) = \{", re.M)


def suite_of_file(text: str) -> str | None:
    m = SUITE_DEF.search(text)
    return m.group(1) if m else None
