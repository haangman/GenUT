"""리포트와 터미널 요약 (설계 7.10, 11.1)."""

from __future__ import annotations

import json
from pathlib import Path

from genut.state import CaseStatus, FunctionState, FunctionStatus, RunState, SavedNote

LABEL = {
    CaseStatus.PASS: "통과",
    CaseStatus.DROPPED: "제거",
    CaseStatus.SUSPECTED_BUG: "소스 버그 의심",
    CaseStatus.FLAKY: "불안정",
    CaseStatus.NOT_RUN: "미실행",
    CaseStatus.FAIL: "실패",
    CaseStatus.CRASH: "크래시",
    CaseStatus.NEW: "미검증",
}


def classify_result(fn: FunctionState, coverage_target: float = 100.0) -> str:
    """complete | partial | none (설계 7.10)."""
    passed = fn.passed()
    if not passed:
        return "none"
    failed_out = [c for c in fn.cases if c.status not in (CaseStatus.PASS, CaseStatus.SUSPECTED_BUG)]
    cov_ok = fn.coverage is None or fn.coverage.percent + 1e-9 >= coverage_target
    return "complete" if not failed_out and cov_ok else "partial"


def counts(fn: FunctionState) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in fn.cases:
        out[c.status.value] = out.get(c.status.value, 0) + 1
    return out


def function_line(fn: FunctionState) -> str:
    if fn.status == FunctionStatus.UNTESTABLE:
        return f"{fn.name}: 테스트 불가 ({fn.status_reason})"
    c = counts(fn)
    parts = [f"통과 {c.get('pass', 0)}"]
    for st in (CaseStatus.DROPPED, CaseStatus.SUSPECTED_BUG, CaseStatus.NOT_RUN, CaseStatus.FLAKY):
        if c.get(st.value):
            parts.append(f"{LABEL[st]} {c[st.value]}")
    cov = f" · 커버리지 {fn.coverage.percent:.0f}% ({fn.coverage.covered}/{fn.coverage.total})" if fn.coverage and fn.coverage.total else ""
    final = {"ok": "최종 확인 OK", "partial": "최종 확인 일부(제거 뒤 재확인 못 함)", "snapshot": "녹색 스냅샷 사용", "unverified": "미확인", "none": "파일 없음"}.get(fn.final_check, "")
    tail = f" · {final}" if final else ""
    return f"{fn.name}: {fn.result or '-'} · " + " · ".join(parts) + cov + tail


def render_report(state: RunState, budget: dict | None = None) -> str:
    lines = [f"# GenUT 리포트: {state.source_file}", "", f"- 실행: {state.run_id}", f"- 백엔드: {state.backend}"]
    if budget:
        lines.append(f"- 빌드 {budget.get('builds_used')}/{budget.get('builds_max')} · 소요 {budget.get('elapsed_sec')}s · 토큰 {sum(budget.get('tokens', {}).values())}")
    if state.interrupted:
        lines.append("- **중단됨**: 그때까지 통과한 케이스로 마무리했습니다. `genut finalize`로 다시 확인할 수 있습니다.")
    lines.append("")
    for fn in state.functions.values():
        lines.append(f"## {fn.name}")
        lines.append("")
        lines.append(f"- 결과: **{fn.result or '-'}** — {function_line(fn)}")
        if fn.installed_path:
            lines.append(f"- 테스트 파일: `{fn.installed_path}`")
        passed = fn.passed()
        if passed:
            lines.append("- 통과 케이스: " + ", ".join(f"`{c.name}`" for c in passed))
        for st in (CaseStatus.DROPPED, CaseStatus.FLAKY, CaseStatus.NOT_RUN, CaseStatus.FAIL, CaseStatus.CRASH):
            for c in fn.by_status(st):
                why = c.drop_reason or (c.last_failure.summary if c.last_failure else "")
                lines.append(f"- {LABEL[st]}: `{c.name}` — {why}")
        bugs = fn.by_status(CaseStatus.SUSPECTED_BUG)
        if bugs:
            lines.append("- 소스 버그 의심 (사람이 확인할 가치가 있음):")
            for c in bugs:
                lines.append(f"  - `{c.name}`: {c.drop_reason}")
        if fn.coverage and fn.coverage.uncovered_lines:
            reasons = {u.line: u for u in fn.unreachable}
            items = []
            for ln in fn.coverage.uncovered_lines:
                u = reasons.get(ln)
                items.append(f":{ln} {u.category}" if u else f":{ln}")
            lines.append("- 미커버 줄: " + ", ".join(items))
        for n in fn.narrowed:
            lines.append(f"- 범위 축소: {n}")
        for n in fn.review_notes:
            lines.append(f"- 리뷰: {n}")
        lines.append("")
    if state.knowledge_notes:
        lines += ["## 지식 노트", ""] + [f"- {note_line(n)}" for n in state.knowledge_notes] + [""]
    if state.notes:
        lines += ["## 참고", ""] + [f"- {n}" for n in state.notes] + [""]
    return "\n".join(lines)


NOTE_SCOPE = {"product": "프로덕트", "candidate": "공통 후보", "global": "공통"}


def note_line(n: SavedNote) -> str:
    """이번 실행이 남긴 노트 한 줄: 저장 범위와, 제안과 달라진 이유."""
    why = ""
    if n.proposed == "global" and n.scope == "product":
        why = f" — 프로덕트 고유 정보 때문에 프로덕트로: {', '.join(n.matched)}"
    elif n.promoted:
        why = " — 다른 프로덕트에도 있어 공통으로 승격"
    return f"[{NOTE_SCOPE.get(n.scope, n.scope)}] {n.text} (id {n.id}){why}"


def render_summary(run_path: Path) -> str:
    sp = run_path / "state.json"
    if not sp.exists():
        return f"{run_path}: state.json이 없습니다 (실행이 시작 전에 멈췄을 수 있음)."
    state = RunState.load(sp)
    budget = {}
    rj = run_path / "run.json"
    if rj.exists():
        budget = json.loads(rj.read_text(encoding="utf-8")).get("budget", {})
    head = f"{state.run_id} · {state.source_file} · {state.backend} · 단계 {state.stage}" + (" · 중단됨" if state.interrupted else "")
    lines = [head]
    if budget:
        lines.append(f"빌드 {budget.get('builds_used')}/{budget.get('builds_max')} · 소요 {budget.get('elapsed_sec')}s · 토큰 {sum(budget.get('tokens', {}).values())}")
    lines += ["  " + function_line(f) for f in state.functions.values()]
    lines += [f"  참고: {n}" for n in state.notes]
    return "\n".join(lines)
