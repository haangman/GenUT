"""Test Writer 에이전트 (설계 3.2 ④⑤, 4.2, 7.4).

LLM은 케이스 본문만 쓴다. 뼈대(include, 스위트, 스텁 연결)는 코드가 만든다.
응답은 그대로 믿지 않고 코드가 정리한다.
- 통과 케이스와 같은 이름의 케이스는 버린다 (통과 케이스는 글자 하나 바꾸지 않는다)
- 이름은 C 식별자로 고치고, 겹치면 _2, _3을 붙인다
- 헬퍼는 공유 영역으로 분리하고 어느 케이스가 쓰는지 기록한다. 이미 있는 헬퍼를 바꾸는 제안과
  아무 케이스도 쓰지 않는 헬퍼는 받지 않는다
소스 버그 의심 목록은 대상 함수 소스의 text_hash로 찾는다 (본문이 바뀌면 무효, 설계 11.5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel

from genut.agents.base import Agent, AgentError, core_section, fit
from genut.analysis.model import AnalysisReport
from genut.analysis.summarizer import report_fragments
from genut.knowledge import KnowledgeStore
from genut.llm.prompt_builder import OPTIONAL, REQUIRED, SHRINKABLE, Fragment
from genut.llm.provider import Usage
from genut.llm.structured import FencedSpec
from genut.state import TERMINAL, CaseStatus, FunctionState, SharedArea, TestCase, text_hash, used_helpers

MODES = ("smoke", "expand", "coverage")


class WrittenCase(BaseModel):
    name: str
    purpose: str
    body: str


class Helper(BaseModel):
    name: str
    code: str


class TestWriterOutput(BaseModel):
    __test__ = False  # pytest가 테스트 클래스로 수집하지 않게

    cases: list[WrittenCase]
    helpers: list[Helper]


@dataclass
class WriteResult:
    cases: list[TestCase]
    helpers: dict[str, str]
    notes: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


def identifier(name: str, fallback: str) -> str:
    s = re.sub(r"\W+", "_", name.strip(), flags=re.ASCII).strip("_")
    if not s:
        return fallback
    return f"{fallback}_{s}" if s[0].isdigit() else s


# ---------------------------------------------------------------- fragments
def _analyzer_notes(fs: FunctionState, brief: bool) -> str:
    lines: list[str] = []
    if fs.cautions:
        lines += ["주의 사항:"] + [f"- {c}" for c in fs.cautions]
    if fs.recommended:
        lines.append("추천 케이스:")
        for r in fs.recommended:
            head = f"- {r.get('name', '')} [{r.get('kind', '')}]"
            lines.append(head if brief else f"{head} {r.get('purpose', '')} → {r.get('expect', '')}")
    return "\n".join(lines)


def helpers_text(shared: SharedArea, signatures: bool) -> str:
    if signatures:
        return "\n".join(code.split("{", 1)[0].strip() + ";" for code in shared.helpers.values())
    return "\n\n".join(shared.helpers.values())


def _passed(cases: list[TestCase], full: bool) -> str:
    if full:
        return "\n\n".join(f"// {c.name}: {c.purpose}\n{c.body}" for c in cases)
    return "\n".join(f"- {c.name}: {c.purpose}" for c in cases) + "\n(소스는 생략했습니다. 이 케이스들은 그대로 유지됩니다.)"


class TestWriter(Agent[TestWriterOutput]):
    __test__ = False

    role = "test_writer"
    output = TestWriterOutput
    fenced = FencedSpec(code="body", items="cases", key="name")

    def fragments(
        self,
        mode: str,
        report: AnalysisReport,
        fs: FunctionState,
        stub_usage: str = "",
        knowledge: KnowledgeStore | None = None,
        uncovered: str = "",
    ) -> list[Fragment]:
        """uncovered: coverage 모드에서 미커버 줄과 도달 조건 (설계 7.6, coverage.render_uncovered)."""
        if mode not in MODES:
            raise ValueError(f"알 수 없는 작성 모드: {mode}")
        if mode == "coverage" and not uncovered.strip():
            raise ValueError("coverage 모드에는 미커버 줄이 필요합니다")
        cfg, counter = self.env.config, self.env.counter
        fn = report.function
        existing = ", ".join(c.name for c in fs.cases) or "(없음)"
        values = self.values(fn.display, function_id=fs.name, existing=existing)
        frags = self.base_fragments(values)
        frags += fit(report_fragments(report), cfg.budget.analysis_token_budget, counter)
        if fs.cautions or fs.recommended:
            frags.append(Fragment("analyzer", _analyzer_notes(fs, False), SHRINKABLE, title="Analyzer 의견",
                                  shrinkers=[lambda: _analyzer_notes(fs, True)]))
        if fs.shared.helpers:
            frags.append(Fragment("helpers", helpers_text(fs.shared, False), SHRINKABLE, title="공유 헬퍼 (이미 있음. 다시 정의하지 말 것)",
                                  shrinkers=[lambda: helpers_text(fs.shared, True)]))
        passed = fs.passed()
        if passed:
            full = _passed(passed, True)
            fits = counter(full) <= cfg.budget.passed_cases_token_budget
            frags.append(Fragment("passed", full if fits else _passed(passed, False), SHRINKABLE, title="통과 케이스 (글자 하나 바꾸지 말 것)",
                                  shrinkers=[lambda: _passed(passed, False)] if fits else []))
        dropped = [c for c in fs.cases if c.status in TERMINAL]
        if dropped:
            frags.append(Fragment("dropped", "\n".join(f"- {c.name}: {c.drop_reason}" for c in dropped), SHRINKABLE,
                                  title="제거된 케이스와 사유 (같은 시도를 반복하지 말 것)", shrinkers=[lambda: ""]))
        if knowledge is not None:
            bugs = knowledge.suspected_bugs(fs.name, text_hash(fn.source))
            if bugs:
                frags.append(Fragment("suspected_bugs", "\n".join(f"- {b['case']}: {b['reason']}" for b in bugs), SHRINKABLE,
                                      title="이전 실행에서 소스 버그 의심으로 제외된 케이스 (다시 시도하지 말 것)", shrinkers=[lambda: ""]))
            budget = cfg.budget.knowledge_token_budget
            notes = knowledge.prompt_block(budget, counter)
            if notes:
                frags.append(Fragment("knowledge", notes, SHRINKABLE, title="지식 노트",
                                      shrinkers=[lambda: knowledge.prompt_block(budget // 2, counter), lambda: ""]))
        if stub_usage.strip():
            core = core_section(stub_usage)
            frags.append(Fragment("stub_usage", stub_usage.strip(), SHRINKABLE, target="system", title="스텁 사용법",
                                  shrinkers=[lambda: core] if core and core != stub_usage.strip() else []))
        if mode == "coverage":
            frags.append(Fragment("uncovered", uncovered.strip(), REQUIRED, title="미커버 줄과 도달 조건"))
        example = self.prompt.render(f"example.{cfg.backend}", values)
        if example:
            frags.append(Fragment("example", example, OPTIONAL, title="예시"))
        frags.append(self.task_fragment(mode, values))
        return frags

    def write(
        self,
        mode: str,
        report: AnalysisReport,
        fs: FunctionState,
        round_: int = 0,
        stub_usage: str = "",
        knowledge: KnowledgeStore | None = None,
        uncovered: str = "",
        covers: list[int] | None = None,
    ) -> WriteResult:
        """케이스를 만들어 fs에 new 상태로 더한다. 새 헬퍼가 있으면 공유 영역 판을 올린다.

        covers: coverage 모드에서 이번에 겨냥한 미커버 줄. 만든 케이스의 covers_lines로 남는다.
        """
        frags = self.fragments(mode, report, fs, stub_usage, knowledge, uncovered)
        try:
            res = self.call(frags, function=fs.name, label=f"{self.role}-{mode}")
        except AgentError as e:
            fs.tokens_used += e.usage.total
            raise
        fs.tokens_used += res.usage.total
        out = merge_cases(res.value, fs, mode, round_)
        out.usage = res.usage
        for c in out.cases:
            c.covers_lines = list(covers or [])
        self._event("cases_written", fs.name, mode=mode, cases=[c.name for c in out.cases], helpers=sorted(out.helpers), notes=out.notes)
        return out


def merge_cases(out: TestWriterOutput, fs: FunctionState, mode: str, round_: int) -> WriteResult:
    notes: list[str] = []
    shared = fs.shared.helpers
    proposed: dict[str, str] = {}
    for h in out.helpers:
        name, code = identifier(h.name, ""), h.code.strip()
        if not name or not code:
            notes.append(f"헬퍼 {h.name!r}: 이름이나 코드가 비어 있어 버렸습니다.")
        elif name in shared:
            if shared[name].strip() != code:
                notes.append(f"헬퍼 {name}: 이미 있는 헬퍼는 바꾸지 않습니다 (공유 영역 보호).")
        else:
            proposed[name] = code

    written = out.cases
    if mode == "smoke" and len(written) > 1:
        notes.append(f"스모크는 케이스 하나만 씁니다. {len(written) - 1}개를 버렸습니다.")
        written = written[:1]
    passed = {c.name for c in fs.passed()}
    added: list[TestCase] = []
    for wc in written:
        body = wc.body.strip("\n")
        if not body.strip():
            notes.append(f"케이스 {wc.name!r}: 본문이 비어 있어 버렸습니다.")
            continue
        name = identifier(wc.name, f"test_{fs.name}")
        if name in passed:
            notes.append(f"케이스 {name}: 통과 케이스와 이름이 같아 버렸습니다 (통과 케이스는 바꾸지 않음).")
            continue
        name = fs.unique_name(name)
        if name != wc.name.strip():
            notes.append(f"케이스 {wc.name!r}: 이름을 {name}(으)로 고쳤습니다.")
        tc = TestCase(name=name, target_function=fs.name, purpose=wc.purpose.strip(), body=body, origin=mode)
        tc.mark(CaseStatus.NEW, round_, mode, "작성")
        fs.cases.append(tc)
        added.append(tc)

    helpers = {**shared, **proposed}
    keep = {n: proposed[n] for n in used_helpers([c.body for c in added], helpers) if n in proposed}
    for n in sorted(set(proposed) - set(keep)):
        notes.append(f"헬퍼 {n}: 쓰는 케이스가 없어 버렸습니다.")
    if keep:
        new = fs.shared.model_copy(deep=True)
        new.helpers.update(keep)
        fs.change_shared(new)
    for tc in added:
        tc.helpers_used = used_helpers([tc.body], fs.shared.helpers)
    return WriteResult(cases=added, helpers=keep, notes=notes)
