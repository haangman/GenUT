"""부분 성공 보존과 최종 확인 (설계 7.10).

통과 케이스만 담은 최종 파일을 그대로 한 번 더 빌드·실행해 확인하고 프로덕트 위치에 설치한다.
최종 확인은 예약 빌드(final_build_reserve)를 쓴다.

| 최종 확인 결과 | 최종 파일 |
| 모두 통과 | 그대로 설치 (ok) |
| 일부 실패, 예약 빌드 남음 | 실패 케이스를 빼고 다시 확인 |
| 일부 실패, 예약 빌드 없음 | 최종 확인에서 통과한 케이스로 설치 (partial: 제거 뒤 재확인 못 함) |
| 빌드 실패·실행 불가·중단 | 케이스가 가장 많은 녹색 스냅샷 (snapshot). 스냅샷에 없는 통과 케이스는 final/에 미확인으로 |
| 녹색 스냅샷도 없음 | final/에 미확인으로만 (unverified). install_unverified면 표시와 함께 설치 |
"""

from __future__ import annotations

from pathlib import Path

from genut.backends.base import Backend, Rendered, TestUnit
from genut.budget import Budget
from genut.state import CaseStatus, RunState
from genut.trace import RunDir, Trace
from genut.verify.diagnostics import LineMap

UNVERIFIED_HEADER = "// GenUT: 최종 확인을 하지 못한 테스트입니다 (미확인). `genut finalize`로 다시 확인할 수 있습니다.\n"


def final_check(
    backend: Backend,
    units: list[TestUnit],
    state: RunState,
    budget: Budget,
    rundir: RunDir,
    install_unverified: bool = False,
    trace: Trace | None = None,
    interrupted: bool = False,
    install: bool = True,
) -> None:
    """install=False면 확인까지만 하고 프로덕트 트리에는 쓰지 않는다 (genut eval). 결과 파일은 final/에 남는다."""
    def event(type_: str, **data: object) -> None:
        if trace is not None:
            trace.event(type_, **data)

    pending: dict[str, TestUnit] = {}
    for u in units:
        if u.state.passed():
            pending[u.function] = u
        else:
            u.state.final_check = "none"
    reduced: set[str] = set()
    attempt = 0
    while pending and not interrupted and budget.can_build(use_reserve=True):
        attempt += 1
        out = rundir.path / "final" / f"check{attempt}"
        rendered = {f: backend.render(u, u.state.passed()) for f, u in pending.items()}
        budget.record_build()
        state.builds_used = budget.builds_used
        b = backend.build(list(rendered.values()), out)
        retry: dict[str, TestUnit] = {}
        broken: list[TestUnit] = []
        for f, u in pending.items():
            r = rendered[f]
            if f not in b.ok_functions:
                broken.append(u)
                continue
            rr = backend.run(r, out)
            failed = [n for n in r.cases if rr.results.get(n) is None or rr.results[n].status != "pass"]
            if not failed:
                _install(backend, rundir, u, r, "partial" if f in reduced else "ok", install)
                continue
            for n in failed:
                case = u.state.case(n)
                if case is not None:
                    case.drop(state.round, "final", "최종 확인에서 실패 (다른 케이스와 함께 돌 때 결과가 달라짐)", CaseStatus.FLAKY)
            reduced.add(f)
            event("final_check_failed", function=f, cases=failed)
            if not u.state.passed():
                u.state.final_check = "none"
            elif budget.can_build(use_reserve=True):
                retry[f] = u
            else:  # 예약 빌드가 없다: 이번 확인에서 통과한 케이스로 확정한다
                _install(backend, rundir, u, backend.render(u, u.state.passed()), "partial", install)
        for u in broken:
            event("final_build_failed", function=u.function)
            _fallback(backend, rundir, state, u, install_unverified, install)
        pending = retry
    for u in pending.values():  # 중단되었거나 예약 빌드가 없다
        _fallback(backend, rundir, state, u, install_unverified, install)


def _install(backend: Backend, rundir: RunDir, u: TestUnit, r: Rendered, check: str, install: bool = True) -> None:
    rundir.write_text(f"final/{r.file_name}", r.text)
    if install:
        u.state.installed_path = str(backend.install(r))
    u.state.final_check = check


def _fallback(backend: Backend, rundir: RunDir, state: RunState, u: TestUnit, install_unverified: bool, install: bool = True) -> None:
    fs = u.state
    passed = {c.name for c in fs.passed()}
    snaps = [s for s in state.snapshots if s.function == fs.name and s.cases and set(s.cases) <= passed]
    full = backend.render(u, fs.passed())
    if snaps:
        best = max(snaps, key=lambda s: (len(s.cases), s.build_no))
        text = (rundir.path / best.path).read_text(encoding="utf-8")
        snap = Rendered(function=fs.name, file_name=full.file_name, text=text, line_map=LineMap(path=full.file_name), cases=list(best.cases), suite=full.suite, source=full.source)
        _install(backend, rundir, u, snap, "snapshot", install)
        rest = sorted(passed - set(best.cases))
        if rest:
            rundir.write_text(f"final/unverified/{full.file_name}", UNVERIFIED_HEADER + full.text)
            fs.review_notes.append("녹색 스냅샷에 없는 통과 케이스는 미확인으로 남았습니다: " + ", ".join(rest))
        return
    rundir.write_text(f"final/{full.file_name}", UNVERIFIED_HEADER + full.text)
    fs.final_check = "unverified"
    if install_unverified and install:
        marked = Rendered(function=fs.name, file_name=full.file_name, text=UNVERIFIED_HEADER + full.text, line_map=full.line_map, cases=full.cases, suite=full.suite, source=full.source)
        fs.installed_path = str(backend.install(marked))


def load_units(run_path: Path, state: RunState) -> list[TestUnit]:
    """finalize·replay용: 실행 기록의 analysis/<함수>.json으로 렌더링에 필요한 단위를 되살린다."""
    from genut.analysis.model import AnalysisReport

    units = []
    for name, fs in state.functions.items():
        p = run_path / "analysis" / f"{name}.json"
        if p.exists():
            units.append(TestUnit(source=Path(state.source_file), report=AnalysisReport.model_validate_json(p.read_text(encoding="utf-8")), state=fs))
    return units
