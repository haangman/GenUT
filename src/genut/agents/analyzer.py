"""Analyzer 에이전트 (설계 3.2 ①, 4.2).

코드가 만든 분석 보고서를 읽고 테스트 가능 여부, 주의 사항, 추천 케이스를 덧붙인다.
"테스트 불가" 판정은 코드가 확인한 결정적 신호가 있을 때만 받아들인다. 신호가 없으면 경고로만
남기고 진행한다. LLM 오판 하나로 실행이 끝나지 않게 하기 위해서다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from genut.agents.base import Agent, AgentError, fit
from genut.analysis.model import AnalysisReport, FunctionInfo
from genut.analysis.summarizer import report_fragments
from genut.llm.prompt_builder import REQUIRED, SHRINKABLE, Fragment
from genut.state import FunctionState, FunctionStatus, UnreachableLine


class RecommendedCase(BaseModel):
    name: str
    kind: Literal["normal", "error", "boundary", "null", "other"]
    purpose: str
    expect: str


class AnalyzerOutput(BaseModel):
    testable: bool
    reason: str
    cautions: list[str]
    recommended: list[RecommendedCase]


def decisive_signals(fn: FunctionInfo, has_compile_entry: bool = True) -> list[str]:
    """테스트 불가를 뒷받침하는, 코드가 확인한 신호."""
    out = []
    if fn.only_asm:
        out.append("본문이 인라인 어셈블리뿐")
    if not has_compile_entry:
        out.append("컴파일 DB 항목 없음")
    if fn.empty_body and not fn.branches and not fn.returns:
        out.append("분기와 반환 지점이 없는 빈 함수")
    return out


@dataclass
class Testability:
    testable: bool
    reason: str = ""
    warning: str = ""


def judge_testability(out: AnalyzerOutput, fn: FunctionInfo, has_compile_entry: bool = True) -> Testability:
    if out.testable:
        return Testability(True)
    signals = decisive_signals(fn, has_compile_entry)
    if signals:
        return Testability(False, reason="; ".join(signals) + (f" — {out.reason}" if out.reason else ""))
    return Testability(True, warning=f"Analyzer가 테스트 불가로 판정했지만 코드가 확인한 신호가 없어 진행합니다: {out.reason}")


class Analyzer(Agent[AnalyzerOutput]):
    role = "analyzer"
    output = AnalyzerOutput

    def fragments(self, report: AnalysisReport) -> list[Fragment]:
        values = self.values(report.function.name)
        analysis = fit(report_fragments(report), self.env.config.budget.analysis_token_budget, self.env.counter)
        return self.base_fragments(values) + analysis + [self.task_fragment("task", values)]

    def run(self, report: AnalysisReport, fs: FunctionState, has_compile_entry: bool = True) -> Testability:
        """분석 결과를 fs(주의 사항, 추천 케이스, 상태, 토큰)에 반영하고 판정을 돌려준다."""
        fn = report.function
        try:
            res = self.call(self.fragments(report), function=fn.name)
        except AgentError as e:
            fs.tokens_used += e.usage.total
            raise
        fs.tokens_used += res.usage.total
        fs.cautions = [c.strip() for c in res.value.cautions if c.strip()]
        fs.recommended = [c.model_dump() for c in res.value.recommended]
        verdict = judge_testability(res.value, fn, has_compile_entry)
        if not verdict.testable:
            fs.status = FunctionStatus.UNTESTABLE
            fs.status_reason = verdict.reason
        self._event("testability", fn.name, testable=verdict.testable, reason=verdict.reason, warning=verdict.warning)
        return verdict


# ------------------------------------------------------------- 도달 불가 분류
class UnreachableItem(BaseModel):
    line: int
    category: Literal["unreachable_defensive", "needs_stub_behavior", "config_dependent", "optimized_out", "unknown"]
    reason: str


class UnreachableOutput(BaseModel):
    lines: list[UnreachableItem]


class UnreachableAnalyzer(Agent[UnreachableOutput]):
    """커버리지 라운드를 거쳐도 남은 미커버 줄을 사유별로 분류한다 (설계 7.6). Analyzer의 두 번째 일."""

    role = "unreachable"
    output = UnreachableOutput

    def classify(self, report: AnalysisReport, fs: FunctionState, uncovered: str, lines: list[int], stub_usage: str = "") -> list[UnreachableLine]:
        values = self.values(report.function.name)
        frags = self.base_fragments(values)
        frags += fit(report_fragments(report), self.env.config.budget.analysis_token_budget, self.env.counter)
        frags.append(Fragment("uncovered", uncovered, REQUIRED, title="미커버 줄과 도달 조건"))
        if stub_usage.strip():
            frags.append(Fragment("stub_usage", stub_usage.strip(), SHRINKABLE, title="스텁 상황", shrinkers=[lambda: ""]))
        frags.append(self.task_fragment("task", values))
        try:
            res = self.call(frags, function=fs.name)
        except AgentError as e:
            fs.tokens_used += e.usage.total
            raise
        fs.tokens_used += res.usage.total
        by_line = {i.line: i for i in res.value.lines}
        return [
            UnreachableLine(line=ln, category=by_line[ln].category, reason=by_line[ln].reason.strip()) if ln in by_line
            else UnreachableLine(line=ln, category="unknown", reason="Analyzer가 분류하지 않았습니다")
            for ln in lines
        ]
