"""LLM에 넣을 분석 보고서 (설계 6.5). 토큰 예산 안에서 압축된 구조화 텍스트."""

from __future__ import annotations

from genut.analysis.model import AnalysisReport, FunctionInfo, Scope
from genut.llm.prompt_builder import REQUIRED, SHRINKABLE, Fragment


def numbered_source(fn: FunctionInfo) -> str:
    lines = fn.source.splitlines()
    width = len(str(fn.start_line + len(lines)))
    return "\n".join(f"{fn.start_line + i:>{width}}| {line}" for i, line in enumerate(lines))


def _target(r: AnalysisReport) -> str:
    fn = r.function
    flags = ["static" if fn.is_static else "비static"]
    if fn.is_inline:
        flags.append("inline")
    if fn.is_method:
        kind = "생성자" if fn.is_constructor else ("static 멤버 함수" if fn.is_static_method else ("const 멤버 함수" if fn.is_const else "멤버 함수"))
        flags.append(f"{fn.access + ' ' if fn.access else ''}{kind} of {fn.owner.qualified_name if fn.owner else fn.class_name}")
    if fn.namespace:
        flags.append(f"namespace {fn.namespace}")
    flags.append(f"{fn.line_count}줄 · 분기 {fn.branch_count}")
    out = [f"{fn.signature}   @ {fn.file}:{fn.start_line}-{fn.end_line}", " · ".join(flags)]
    if fn.preproc:
        out.append("빌드 조건: " + " && ".join(fn.preproc))
    return "\n".join(out)


def call_hint(fn: FunctionInfo) -> str:
    """테스트에서 대상을 부르는 방법 (C++). C 함수면 빈 문자열."""
    if fn.is_method and fn.owner is not None:
        cls = fn.owner.qualified_name
        if fn.is_constructor:
            return f"`{fn.display}`는 {cls}의 생성자다. 케이스 안에서 이 생성자로 객체를 만들고, 공개 멤버 함수로 만들어진 상태를 확인한다."
        if fn.is_static_method:
            return f"`{fn.display}`는 {cls}의 static 멤버 함수다. 객체 없이 `{cls}::{fn.name}(...)`로 부른다."
        if fn.fixture_object:
            return (f"`{fn.display}`는 {cls}의 인스턴스 멤버 함수다. fixture가 기본 생성한 객체 `obj`를 준다(`obj.{fn.name}(...)`). "
                    "다른 생성자나 상태가 필요하면 케이스 안에서 공개 생성자와 공개 멤버 함수로 만든다. 비공개 멤버 변수는 직접 건드리지 않는다.")
        return f"`{fn.display}`는 {cls}의 인스턴스 멤버 함수다. 케이스 안에서 공개 생성자로 객체를 만든 뒤 `<객체>.{fn.name}(...)`로 부른다."
    if fn.namespace:
        return f"`{fn.name}`는 namespace {fn.namespace} 안에 있다. `{fn.namespace}::{fn.name}(...)`로 부른다."
    return ""


def _class(fn: FunctionInfo, brief: bool) -> str:
    o = fn.owner
    if o is None:
        return ""
    traits = ["기본 생성 가능" if o.default_constructible else "기본 생성자 없음"] + (["추상 클래스"] if o.abstract else [])
    out = [f"{o.kind} {o.qualified_name}   @ {o.file}:{o.line}   ({', '.join(traits)})",
           "공개 생성자: " + (" · ".join(o.constructors) or "(선언 없음: 암묵적 기본 생성자)"),
           "공개 멤버 함수: " + (" · ".join(o.methods) or "(없음)")]
    if not brief and o.fields:
        out.append("멤버 변수: " + " · ".join(o.fields))
    return "\n".join(out)


def _types(fn: FunctionInfo, mode: str) -> str:
    if not fn.types:
        return ""
    out = []
    param_records = {p.record for p in fn.params if p.record}
    for t in fn.types:
        if mode == "names":
            out.append(f"{t.name} ({t.kind})")
            continue
        if mode == "related" and t.name not in param_records and t.kind != "enum":
            continue
        out.append(f"{t.text}   @ {t.file}:{t.line}")
        if t.example_init and mode == "full":
            out.append(f"예시 초기화: {t.example_init}")
    return "\n".join(out)


def _returns(fn: FunctionInfo) -> str:
    if not fn.returns:
        return "(반환문 없음)"
    w = max(len(r.expr) for r in fn.returns)
    return "\n".join(f"{r.expr or '(void)':<{w}}  {('(' + r.condition + ')') if r.condition else '(조건 없음)'}  :{r.line}" for r in fn.returns)


def _calls(r: AnalysisReport) -> str:
    if not r.linkscope:
        indirect = [c.name for c in r.function.calls if c.indirect]
        return "(호출 없음)" + (f" · 간접 호출: {', '.join(indirect)}" if indirect else "")
    lines = []
    for j in r.linkscope:
        mark = " → 스텁 필요" if j.needs_stub else ""
        st = " (static)" if j.is_static else ""
        lines.append(f"{j.name}{st}  {j.scope.value}{mark}  — {j.reason}")
    indirect = [c.name for c in r.function.calls if c.indirect]
    if indirect:
        lines.append("간접 호출(함수 포인터): " + ", ".join(indirect))
    return "\n".join(lines)


def _globals(fn: FunctionInfo, names_only: bool) -> str:
    if not fn.globals:
        return ""
    if names_only:
        return ", ".join(g.name for g in fn.globals)
    return "\n".join(f"{g.type} {g.name}  {g.access}{' · static' if g.is_static else ''}" for g in fn.globals)


def _constants(fn: FunctionInfo, limit: int | None) -> str:
    items = list(fn.constants.items())
    if limit is not None:
        items = items[:limit]
    return "\n".join(f"{k} = {v}" for k, v in items)


def report_fragments(r: AnalysisReport) -> list[Fragment]:
    fn = r.function
    frags = [
        Fragment("analysis.target", _target(r), REQUIRED, title="대상"),
        Fragment("analysis.call", call_hint(fn), REQUIRED, title="호출 방법"),
        Fragment("analysis.class", _class(fn, False), SHRINKABLE, title="클래스", shrinkers=[lambda: _class(fn, True)]),
        Fragment("analysis.source", numbered_source(fn), REQUIRED, title="소스 (줄 번호 포함)"),
        Fragment("analysis.returns", _returns(fn), REQUIRED, title="반환 지점"),
        Fragment("analysis.calls", _calls(r), REQUIRED, title="호출과 링크 범위"),
        Fragment("analysis.includes", "\n".join(fn.includes) or "(대상 헤더 없음: static 함수거나 선언이 소스 안에 있음)", REQUIRED, title="include"),
    ]
    if fn.types:
        frags.append(
            Fragment("analysis.types", _types(fn, "full"), SHRINKABLE, title="타입",
                     shrinkers=[lambda: _types(fn, "related"), lambda: _types(fn, "names")])
        )
    if fn.globals:
        frags.append(Fragment("analysis.globals", _globals(fn, False), SHRINKABLE, title="전역 참조", shrinkers=[lambda: _globals(fn, True)]))
    if fn.constants:
        frags.append(Fragment("analysis.constants", _constants(fn, None), SHRINKABLE, title="상수", shrinkers=[lambda: _constants(fn, 15), lambda: ""]))
    return frags


def render_report(r: AnalysisReport) -> str:
    """analysis.md: LLM에 들어가는 것과 같은 내용 + 판정 근거."""
    parts = [f"# 분석 보고서: {r.function.display}", ""]
    for f in report_fragments(r):
        if f.text:
            parts += [f"## {f.title}", "", "```", f.text, "```", ""]
    if r.dropped_flags:
        parts += ["## 정제로 뺀 컴파일 플래그", "", " ".join(r.dropped_flags), ""]
    if r.parse_errors:
        parts += ["## 파싱 오류", ""] + [f"- {e}" for e in r.parse_errors] + [""]
    stub = [j for j in r.linkscope if j.scope in (Scope.UNDEFINED, Scope.MUST_STUB)]
    if stub:
        parts += ["## 스텁 필수 심볼", ""] + [f"- `{j.name}`: {j.reason} {('(' + j.evidence + ')') if j.evidence else ''}" for j in stub] + [""]
    return "\n".join(parts)
