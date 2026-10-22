"""Reviewer 에이전트 (설계 3.2 ⑧, 4.2, 7.6, 7.9).

코드 리뷰 검사(단정 없음, 항상 참, 금지 패턴, 대상 미호출, 실제값 베끼기)를 통과한 케이스를 대상으로,
코드로 판단하기 어려운 것만 본다: 규칙의 의도, 의미 있는 단정인가, 수정 이력에 남은 단정 약화,
커버리지용 케이스가 도달한 경로의 결과를 확인하는가. 지적은 구체적 근거와 함께 받는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from genut.agents.base import Agent, AgentError, fit
from genut.analysis.model import AnalysisReport
from genut.analysis.summarizer import report_fragments
from genut.llm.prompt_builder import REQUIRED, SHRINKABLE, Fragment
from genut.llm.provider import Usage
from genut.state import CaseStatus, FunctionState, TestCase

KINDS = ("rule_violation", "meaningless_assertion", "weakened_assertion", "coverage_without_check")


class ReviewFinding(BaseModel):
    case: str
    kind: Literal["rule_violation", "meaningless_assertion", "weakened_assertion", "coverage_without_check"]
    detail: str


class ReviewerOutput(BaseModel):
    findings: list[ReviewFinding]


@dataclass
class ReviewResult:
    findings: list[ReviewFinding]
    ignored: list[str] = field(default_factory=list)  # 통과 케이스가 아니거나 없는 케이스에 대한 지적
    usage: Usage = field(default_factory=Usage)


def _case_text(c: TestCase) -> str:
    tag = f"[{c.origin}" + (f", 미커버 줄 {','.join(map(str, c.covers_lines))}을 겨냥" if c.covers_lines else "") + "]"
    return f"// {c.name} {tag}: {c.purpose}\n{c.body}"


def _history_text(c: TestCase) -> str:
    if not c.previous_body:
        return ""
    lines = [f"### {c.name} (수정 {c.fixes_used}회)"]
    if c.last_failure is not None:
        lines.append(f"고치기 전 실패: {c.last_failure.category} — {c.last_failure.summary}")
    lines += ["고치기 전 본문:", c.previous_body]
    if c.fix_evidence:
        lines.append(f"Fixer가 댄 근거: {c.fix_evidence}")
    return "\n".join(lines)


class Reviewer(Agent[ReviewerOutput]):
    role = "reviewer"
    output = ReviewerOutput

    def fragments(self, report: AnalysisReport, fs: FunctionState) -> list[Fragment]:
        cfg, counter = self.env.config, self.env.counter
        values = self.values(report.function.display, function_id=fs.name)
        passed = fs.passed()
        frags = self.base_fragments(values)
        frags += fit(report_fragments(report), cfg.budget.analysis_token_budget, counter)
        frags.append(Fragment("cases", "\n\n".join(_case_text(c) for c in passed), REQUIRED, title="통과 케이스"))
        history = "\n\n".join(t for t in (_history_text(c) for c in passed) if t)
        if history:
            frags.append(Fragment("history", history, SHRINKABLE, title="수정 이력 (단정 약화를 찾을 근거)", shrinkers=[lambda: ""]))
        frags.append(self.task_fragment("task", values))
        return frags

    def review(self, report: AnalysisReport, fs: FunctionState) -> ReviewResult:
        """지적 목록을 돌려준다. 통과 케이스에 대한 지적만 받는다."""
        if not fs.passed():
            return ReviewResult([])
        try:
            res = self.call(self.fragments(report, fs), function=fs.name)
        except AgentError as e:
            fs.tokens_used += e.usage.total
            raise
        fs.tokens_used += res.usage.total
        passed = {c.name for c in fs.cases if c.status == CaseStatus.PASS}
        keep, ignored, seen = [], [], set()
        for f in res.value.findings:
            if f.case not in passed:
                ignored.append(f.case)
            elif f.case not in seen:  # 케이스당 첫 지적만
                seen.add(f.case)
                keep.append(f)
        return ReviewResult(keep, ignored, res.usage)
