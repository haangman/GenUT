"""Note Keeper 에이전트 (설계 4.2, 11.5).

실행이 끝난 뒤 실행 요약(판정, 실패 분류, 수정 결과)을 보고 다음 생성에 도움이 될 사실을 한 문장씩 제안한다.
LLM은 범위(global | product)를 제안만 하고, 범위는 코드가 정한다(KnowledgeStore.add_note의 범위 검증).
코드가 하는 검사:
- 근거(evidence)가 이번 실행의 함수·케이스를 가리키지 않으면 버린다
- 실패 분류가 이번 실행에 없던 것이면 비우고, 전략은 전략 사다리 안의 것만 남긴다
- 너무 긴 노트, 같은 문장, 상한(max_notes_per_run)을 넘는 노트는 버린다
배울 것이 없는 실행(모든 케이스가 첫 시도에 통과)에는 부르지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from genut.agents.base import Agent
from genut.llm.prompt_builder import REQUIRED, SHRINKABLE, Fragment
from genut.llm.provider import Usage
from genut.state import CaseStatus, FunctionState, FunctionStatus, RunState
from genut.verify.classify import LADDER

MAX_NOTE_CHARS = 300
_LABEL = {CaseStatus.PASS: "통과", CaseStatus.DROPPED: "제거", CaseStatus.SUSPECTED_BUG: "소스 버그 의심", CaseStatus.FLAKY: "불안정",
          CaseStatus.NOT_RUN: "미실행", CaseStatus.FAIL: "실패", CaseStatus.CRASH: "크래시", CaseStatus.NEW: "미검증"}


class NoteProposal(BaseModel):
    text: str
    scope: Literal["global", "product"]
    category: str
    strategy: str
    evidence: str


class NoteKeeperOutput(BaseModel):
    notes: list[NoteProposal]


@dataclass
class FixRecord:
    """수정 한 번의 결과. 오케스트레이터가 모은다."""

    function: str
    case: str
    category: str
    summary: str
    strategy: str  # 실제로 적용된 전략
    reason: str = ""


@dataclass
class NoteResult:
    accepted: list[NoteProposal]
    rejected: list[tuple[NoteProposal, str]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


# ------------------------------------------------------------------ 실행 요약
def _first_try(fs: FunctionState, fixed: set[str]) -> list[str]:
    return [c.name for c in fs.cases if c.status == CaseStatus.PASS and c.fixes_used == 0 and c.review_fixes_used == 0 and c.name not in fixed]


def has_lessons(state: RunState, fixes: list[FixRecord]) -> bool:
    """배울 것이 있는 실행인가: 수정, 실패했던·제외·의심·불안정 케이스, 범위 축소, 미커버 줄 분류 중 하나라도 있으면.
    테스트 불가 판정만 있는 실행은 아니다(코드 규칙이나 Analyzer 판정이라 노트로 남길 시행착오가 없다)."""
    if fixes:
        return True
    for fs in state.functions.values():
        if fs.narrowed or fs.unreachable:
            return True
        if any(c.status not in (CaseStatus.PASS, CaseStatus.NEW) or c.last_failure is not None for c in fs.cases):
            return True
    return False


def function_digest(fs: FunctionState, display: str, fixes: list[FixRecord], full: bool = True) -> str:
    """함수 하나의 실행 요약. full=False면 고치기 전후 본문을 뺀다."""
    mine = [f for f in fixes if f.function == fs.name]
    fixed = {f.case for f in mine}
    head = f"## {display}" + (f" (키 {fs.name})" if display != fs.name else "")
    lines = [head]
    if fs.status == FunctionStatus.UNTESTABLE:
        lines.append(f"- 테스트 불가: {fs.status_reason}")
    else:
        passed = len(fs.passed())
        cov = f" · 커버리지 {fs.coverage.percent:.0f}% ({fs.coverage.covered}/{fs.coverage.total})" if fs.coverage and fs.coverage.total else ""
        lines.append(f"- 결과: {fs.result or '-'} · 통과 {passed}/{len(fs.cases)}{cov}")
    lines += [f"- Analyzer 주의: {c}" for c in fs.cautions]
    first = _first_try(fs, fixed)
    if first:
        lines.append("- 첫 시도에 통과: " + ", ".join(first))
    for c in fs.cases:
        if c.name in first:
            continue
        state = _LABEL.get(c.status, c.status.value)
        why = f" — {c.drop_reason}" if c.drop_reason else ""
        lines.append(f"- {c.name}: {state}" + (f" (수정 {c.fixes_used}회)" if c.fixes_used else "") + why)
        recs = [f for f in mine if f.case == c.name]
        for f in recs:
            lines.append(f"  - 실패 {f.category}: {f.summary}")
            lines.append(f"  - 적용한 전략 {f.strategy}" + (f": {f.reason}" if f.reason else ""))
        if not recs and c.last_failure is not None:
            lines.append(f"  - 실패 {c.last_failure.category}: {c.last_failure.summary}")
        if full and c.status == CaseStatus.PASS and c.previous_body:
            lines += ["  고치기 전:", "```", c.previous_body.strip(), "```", "  고친 뒤:", "```", c.body.strip(), "```"]
    for u in fs.unreachable:
        lines.append(f"- 미커버 :{u.line} {u.category}" + (f" — {u.reason}" if u.reason else ""))
    lines += [f"- 범위 축소: {n}" for n in fs.narrowed]
    lines += [f"- 리뷰: {n}" for n in fs.review_notes]
    return "\n".join(lines)


def run_categories(state: RunState, fixes: list[FixRecord]) -> set[str]:
    cats = {f.category for f in fixes}
    for fs in state.functions.values():
        cats |= {c.last_failure.category for c in fs.cases if c.last_failure is not None}
    return {c for c in cats if c}


def accept_notes(out: NoteKeeperOutput, state: RunState, fixes: list[FixRecord], displays: dict[str, str], max_notes: int) -> NoteResult:
    names = set(state.functions) | set(displays.values()) | {c.name for fs in state.functions.values() for c in fs.cases}
    names = {n for n in names if n}
    cats = run_categories(state, fixes)
    res = NoteResult([])
    seen: set[str] = set()
    for p in out.notes:
        text = " ".join(p.text.split())
        if not text:
            res.rejected.append((p, "빈 노트"))
            continue
        if len(text) > MAX_NOTE_CHARS:
            res.rejected.append((p, f"{MAX_NOTE_CHARS}자를 넘음"))
            continue
        if text in seen:
            res.rejected.append((p, "같은 노트"))
            continue
        if not any(n in p.evidence for n in names):
            res.rejected.append((p, "근거가 이번 실행의 함수·케이스를 가리키지 않음"))
            continue
        if len(res.accepted) >= max_notes:
            res.rejected.append((p, f"실행당 상한 {max_notes}개를 넘음"))
            continue
        seen.add(text)
        res.accepted.append(NoteProposal(text=text, scope=p.scope, category=p.category if p.category in cats else "",
                                         strategy=p.strategy if p.strategy in LADDER else "", evidence=p.evidence.strip()))
    return res


# ------------------------------------------------------------------ agent
class NoteKeeper(Agent[NoteKeeperOutput]):
    role = "note_keeper"
    output = NoteKeeperOutput
    budget_key = "(notes)"  # 함수가 아니라 실행 단위 호출. 함수별 토큰 예산과 따로 센다

    def fragments(self, state: RunState, displays: dict[str, str], fixes: list[FixRecord], existing: str) -> list[Fragment]:
        n = self.env.config.knowledge.max_notes_per_run
        values = self.values(Path(state.source_file).name, max_notes=str(n))
        frags = [Fragment("role", self.prompt.render(None, values), REQUIRED, target="system"),
                 Fragment("run", f"- 대상 파일: {Path(state.source_file).name}\n- 백엔드: {state.backend}", REQUIRED, title="실행")]
        for key, fs in state.functions.items():
            display = displays.get(key, key)
            full = function_digest(fs, display, fixes, full=True)
            brief = function_digest(fs, display, fixes, full=False)
            frags.append(Fragment(f"digest:{key}", full, SHRINKABLE, title=f"실행 요약: {display}",
                                  shrinkers=[lambda b=brief: b] if brief != full else []))
        if existing.strip():
            frags.append(Fragment("existing", existing, SHRINKABLE, title="이미 있는 노트 (다시 내지 마세요)", shrinkers=[lambda: ""]))
        frags.append(self.task_fragment("task", values))
        return frags

    def propose(self, state: RunState, displays: dict[str, str], fixes: list[FixRecord], existing: str = "") -> NoteResult:
        res = self.call(self.fragments(state, displays, fixes, existing), function=self.budget_key)
        out = accept_notes(res.value, state, fixes, displays, self.env.config.knowledge.max_notes_per_run)
        out.usage = res.usage
        return out
