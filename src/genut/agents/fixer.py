"""Fixer 에이전트 (설계 3.2 ⑥, 4.2, 7.3, 7.4).

코드가 실패를 분류하고 권장 시작 전략을 정한다. Fixer는 그 전략 이상(더 포기하는 쪽)만 고를 수 있고,
고친다면 그 케이스 하나의 본문 전체를 다시 쓴다. 응답 적용(apply_fix)도 코드가 한다.
- 허용되지 않은 전략은 허용 목록의 첫 전략으로 바꾼다
- 스텁 도구가 없으면 add_stub은 범위 축소(narrow_scope)가 된다
- 공유 헬퍼 변경은 공유 영역 판을 올린다. 통과 케이스가 깨져 되돌린 적이 있는 케이스는 다시 바꿀 수 없다
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from genut.agents.base import Agent, AgentError, core_section, fit
from genut.agents.test_writer import Helper, helpers_text, identifier
from genut.analysis.model import AnalysisReport
from genut.analysis.summarizer import report_fragments
from genut.knowledge import KnowledgeStore
from genut.llm.prompt_builder import OPTIONAL, REQUIRED, SHRINKABLE, Fragment
from genut.llm.provider import Usage
from genut.llm.structured import FencedSpec
from genut.models import FailureInfo
from genut.state import CaseStatus, FunctionState, FunctionStatus, TestCase, text_hash
from genut.verify import diagnostics


class FixerOutput(BaseModel):
    strategy: Literal["fix_case", "add_stub", "drop_case", "narrow_scope", "give_up"]
    reason: str
    body: str
    evidence: str
    suspected_bug: bool
    helpers: list[Helper]


def failure_text(fi: FailureInfo, detail: str = "full") -> str:
    """detail: full | brief | summary. 원시 로그는 넣지 않는다(이미 구조화·축약된 것만)."""
    lines = [f"분류: {fi.category} (단계: {fi.stage})"]
    if fi.summary:
        lines.append(f"요약: {fi.summary}")
    if fi.expected is not None or fi.actual is not None:
        lines.append(f"기대값: {fi.expected} · 실제값: {fi.actual}")
    if fi.observed and detail != "summary":
        lines.append("실패 메시지에 나온 값: " + ", ".join(fi.observed))
    if fi.review_check:
        lines.append(f"리뷰 검사: {fi.review_check}")
    if detail == "summary":
        return "\n".join(lines)
    if fi.diagnostics:
        lines += ["진단:", diagnostics.render(fi.diagnostics, 5 if detail == "full" else 2)]
    if fi.crash is not None:
        lines.append(fi.crash.render())
    if fi.details and detail == "full":
        lines += ["상세:", fi.details]
    return "\n".join(lines)


def _examples(items: list[dict]) -> str:
    return "\n\n".join(f"- {i.get('message', '')}\n  전:\n{i.get('before', '')}\n  후:\n{i.get('after', '')}" for i in items)


class Fixer(Agent[FixerOutput]):
    role = "fixer"
    output = FixerOutput
    fenced = FencedSpec(code="body")

    def fragments(
        self,
        report: AnalysisReport,
        fs: FunctionState,
        case: TestCase,
        allowed: list[str],
        stub_usage: str = "",
        knowledge: KnowledgeStore | None = None,
    ) -> list[Fragment]:
        cfg, counter = self.env.config, self.env.counter
        fn = report.function
        fi = case.last_failure or FailureInfo(category="unknown", stage="run")
        hint = self._hint(fi)
        values = self.values(
            fn.name, case=case.name, category=fi.category, hint=hint, allowed=", ".join(allowed),
            shared_rule=self.prompt.render("shared_blocked", {}) if case.shared_change_blocked else "",
        )
        frags = self.base_fragments(values)
        frags += fit(report_fragments(report), cfg.budget.analysis_token_budget, counter)
        if fs.shared.helpers:
            frags.append(Fragment("helpers", helpers_text(fs.shared, False), SHRINKABLE, title="공유 헬퍼",
                                  shrinkers=[lambda: helpers_text(fs.shared, True)]))
        keep = [c.name for c in fs.passed() if c.name != case.name]
        frags.append(Fragment("passed", ", ".join(keep) or "(없음)", REQUIRED, title="통과 케이스 (손대지 말 것)"))
        frags.append(Fragment("case", f"// {case.name}: {case.purpose}\n{case.body}", REQUIRED, title="실패한 케이스"))
        full = failure_text(fi, "full")
        start = full if counter(full) <= cfg.budget.log_token_budget else failure_text(fi, "brief")
        frags.append(Fragment("failure", start, SHRINKABLE, title="실패 정보",
                              shrinkers=[lambda: failure_text(fi, "brief"), lambda: failure_text(fi, "summary")]))
        if case.previous_body:
            frags.append(Fragment("previous", case.previous_body, OPTIONAL, title="직전 본문 (이 본문에서 고친 결과가 위 실패)"))
        if knowledge is not None:
            ex = knowledge.fix_examples(fi.category, limit=3)
            if ex:
                frags.append(Fragment("fix_examples", _examples(ex), SHRINKABLE, title="같은 분류의 과거 수정 사례",
                                      shrinkers=[lambda: _examples(ex[:1]), lambda: ""]))
            bugs = knowledge.suspected_bugs(fn.name, text_hash(fn.source))
            if bugs:
                frags.append(Fragment("suspected_bugs", "\n".join(f"- {b['case']}: {b['reason']}" for b in bugs), SHRINKABLE,
                                      title="이전 실행의 소스 버그 의심", shrinkers=[lambda: ""]))
            budget = cfg.budget.knowledge_token_budget
            notes = knowledge.prompt_block(budget, counter)
            if notes:
                frags.append(Fragment("knowledge", notes, SHRINKABLE, title="지식 노트",
                                      shrinkers=[lambda: knowledge.prompt_block(budget // 2, counter), lambda: ""]))
        if stub_usage.strip():
            core = core_section(stub_usage)
            frags.append(Fragment("stub_usage", stub_usage.strip(), SHRINKABLE, target="system", title="스텁 사용법",
                                  shrinkers=[lambda: core] if core and core != stub_usage.strip() else []))
        frags.append(self.task_fragment("task", values))
        return frags

    @staticmethod
    def _hint(fi: FailureInfo) -> str:
        from genut.verify.classify import Classifier

        c = Classifier().classify_run(fi.category)
        return c.hint or "진단을 보고 그 케이스만 고치세요."

    def fix(
        self,
        report: AnalysisReport,
        fs: FunctionState,
        case: TestCase,
        allowed: list[str],
        stub_usage: str = "",
        knowledge: KnowledgeStore | None = None,
    ) -> tuple[FixerOutput, Usage]:
        """Fixer를 부르고 (출력, 사용 토큰)을 돌려준다. 적용은 apply_fix."""
        try:
            res = self.call(self.fragments(report, fs, case, allowed, stub_usage, knowledge), function=fs.name, label=f"{self.role}-{case.name}")
        except AgentError as e:
            fs.tokens_used += e.usage.total
            raise
        fs.tokens_used += res.usage.total
        return res.value, res.usage


@dataclass
class FixOutcome:
    strategy: str
    retest: bool  # 케이스가 다시 검증 대상(new)이 되었는가
    shared_changed: bool = False
    notes: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


def apply_fix(out: FixerOutput, fs: FunctionState, case: TestCase, allowed: list[str], round_: int, stub_supported: bool) -> FixOutcome:
    notes: list[str] = []
    strategy = out.strategy
    if strategy not in allowed:
        notes.append(f"허용되지 않은 전략 {strategy}을(를) {allowed[0]}(으)로 바꿨습니다.")
        strategy = allowed[0]
    reason = out.reason.strip() or strategy
    case.fixes_used += 1
    if strategy == "add_stub" and not stub_supported:
        case.drop(round_, "fix", f"스텁 도구 연동 필요: {reason}")
        fs.narrowed.append(f"{case.name}: 스텁이 필요한 경로라 다루지 않음 (스텁 도구 연동 필요) — {reason}")
        return FixOutcome("narrow_scope", False, notes=notes)
    if strategy in ("fix_case", "add_stub"):
        body = out.body.strip("\n")
        if not body.strip() or body.strip() == case.body.strip():
            notes.append("새 본문이 없거나 그대로여서 고치지 못했습니다.")
            return FixOutcome(strategy, False, notes=notes)
        case.previous_body = case.body
        case.body = body
        case.fix_evidence = out.evidence.strip()
        case.mark(CaseStatus.NEW, round_, "fix", reason)
        changed = _apply_helpers(out.helpers, fs, case, notes)
        return FixOutcome(strategy, True, changed, notes)
    if strategy == "drop_case":
        status = CaseStatus.SUSPECTED_BUG if out.suspected_bug else CaseStatus.DROPPED
        case.drop(round_, "fix", reason, status)
    elif strategy == "narrow_scope":
        case.drop(round_, "fix", f"범위 축소: {reason}")
        fs.narrowed.append(f"{case.name}: {reason}")
    elif strategy == "give_up":
        give_up(fs, round_, reason)
    return FixOutcome(strategy, False, notes=notes)


def give_up(fs: FunctionState, round_: int, reason: str) -> None:
    """이 함수에 대한 새 시도를 멈춘다. 이미 통과한 케이스는 그대로 최종 파일로 간다 (7.10)."""
    fs.status = FunctionStatus.GIVEN_UP
    fs.status_reason = reason
    for c in fs.live_cases():
        if c.status != CaseStatus.PASS:
            c.drop(round_, "fix", f"give_up: {reason}")


def _apply_helpers(helpers: list[Helper], fs: FunctionState, case: TestCase, notes: list[str]) -> bool:
    proposed = {}
    for h in helpers:
        name, code = identifier(h.name, ""), h.code.strip()
        if name and code and fs.shared.helpers.get(name, "").strip() != code:
            proposed[name] = code
    if not proposed:
        return False
    if case.shared_change_blocked:
        notes.append("이 케이스는 공유 헬퍼를 바꿀 수 없어 헬퍼 변경을 버렸습니다.")
        return False
    new = fs.shared.model_copy(deep=True)
    new.helpers.update(proposed)
    fs.change_shared(new)
    notes.append("공유 헬퍼를 바꿨습니다: " + ", ".join(sorted(proposed)))
    return True
