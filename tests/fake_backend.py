"""오케스트레이터 시나리오 테스트용 가짜 백엔드.

케이스 본문의 표지로 결과를 정한다. 뼈대는 실제 gtest 렌더러를 쓴다.
- CHECK_ERROR: 의미 검사 오류      - COMPILE_ERROR: 그 케이스의 컴파일 오류
- LINK_ERROR: 귀속할 수 없는 링크 오류(@shared, undefined reference)
- FAIL ACTUAL=<값>: 단정 실패        - CRASH: 세그폴트 (뒤 케이스는 not_run)
- TIMEOUT: 시간 초과                - FLAKY: 첫 실행만 통과
- FAILS_IN_FINAL: 최종 확인 실행에서만 실패
- USES_HELPER: 파일에 BREAKS_OTHERS가 있으면 실패 (공유 영역 회귀)
- COVERS=33,34: 통과하면 그 줄을 실행한 것으로 친다. executable_lines를 정한 시나리오에서만 커버리지를 돌려준다
"""

from __future__ import annotations

import re
from pathlib import Path

from genut.analysis.compile_db import CompileDB
from genut.analysis.linkscope import GTestLinkContext
from genut.backends.base import BuildResult, CommandResult, PreflightResult, Rendered, RunResult, TestUnit
from genut.backends.gtest import skeleton
from genut.coverage import FileCoverage
from genut.models import CaseResult, CrashBrief, Diagnostic
from genut.verify.staged import CheckResult


def case_bodies(r: Rendered) -> dict[str, str]:
    lines = r.text.splitlines()
    return {name: "\n".join(lines[s:e - 1]) for name, (s, e) in r.line_map.cases.items()}


class MarkerBackend:
    name = "gtest"

    def __init__(self, db_path: Path, install_root: Path, link_sources: list[Path], preflight_seconds: float = 1.0) -> None:
        self.db_path = db_path
        self.install_root = install_root
        self.link_sources = link_sources
        self.preflight_seconds = preflight_seconds
        self.preflight_ok = True
        self.fail_builds_from = 0  # 이 번호의 빌드부터는 모두 실패 (0이면 끔)
        self.builds: list[list[str]] = []  # 빌드마다 (함수, 케이스 목록) 기록
        self.runs: list[tuple[str, list[str]]] = []
        self.installed: dict[str, str] = {}
        self.executable_lines: list[int] | None = None  # 커버리지를 돌려줄 때 대상 함수의 실행 가능한 줄
        self.entry_lines: set[int] = set()  # 대상 함수를 부르기만 하면 실행되는 줄
        self.coverage_calls: list[list[str]] = []
        self._flaky: dict[str, int] = {}

    def configure(self, units) -> CommandResult:  # noqa: ANN001
        return CommandResult(["cmake"], 0, "", 0.0)

    def compile_db(self) -> CompileDB:
        return CompileDB.load(self.db_path)

    def link_context(self, unit: TestUnit) -> GTestLinkContext:
        return GTestLinkContext(product_root=unit.source.parents[1], link_sources=self.link_sources)

    def fingerprint(self) -> str:
        return "marker"

    def render(self, unit: TestUnit, cases=None) -> Rendered:  # noqa: ANN001
        return skeleton.render(unit, f"{unit.source.stem}_{unit.function}_test.cc", cases)

    def preflight(self, units, out_dir=None) -> PreflightResult:  # noqa: ANN001
        self.builds.append(["preflight"])
        if not self.preflight_ok:
            return PreflightResult(False, 0.5, "boom", category="infra_error", hint="뼈대 빌드 실패")
        return PreflightResult(True, self.preflight_seconds)

    def check(self, r: Rendered) -> CheckResult:
        diags = [Diagnostic(file=r.file_name, line=r.line_map.cases[n][0] + 1, message="use of undeclared identifier 'nope'", stage="check", owner=n)
                 for n, body in case_bodies(r).items() if "CHECK_ERROR" in body]
        return CheckResult(True, diags)

    def build(self, rendered: list[Rendered], out_dir=None) -> BuildResult:  # noqa: ANN001
        self.builds.append([f"{r.function}:{','.join(r.cases)}" for r in rendered])
        ok, failed, diags = [], [], {}
        for r in rendered:
            ds = []
            for n, body in case_bodies(r).items():
                if "COMPILE_ERROR" in body:
                    ds.append(Diagnostic(file=r.file_name, line=r.line_map.cases[n][0] + 1, message="'nope' was not declared in this scope", owner=n))
            if any("LINK_ERROR" in b for b in case_bodies(r).values()):
                ds.append(Diagnostic(stage="link", message="undefined reference to `rb_alloc_slot`", notes=["symbol rb_alloc_slot"], owner="@shared"))
            if self.fail_builds_from and len(self.builds) >= self.fail_builds_from:
                ds.append(Diagnostic(message="ninja: build stopped", owner="@shared"))
            diags[r.function] = ds
            (failed if ds else ok).append(r.function)
        return BuildResult(ok, failed, diags, 0.1)

    def run(self, r: Rendered, out_dir=None, only=None) -> RunResult:  # noqa: ANN001
        bodies = case_bodies(r)
        names = only or r.cases
        self.runs.append((r.function, list(names)))
        final = out_dir is not None and "final" in Path(out_dir).parts
        results: dict[str, CaseResult] = {}
        crash = None
        for i, n in enumerate(names):
            body = bodies[n]
            if "CRASH" in body or "TIMEOUT" in body:
                timeout = "TIMEOUT" in body
                results[n] = CaseResult(name=n, status="timeout" if timeout else "crash", message="시간 초과" if timeout else "segv")
                crash = CrashBrief(category="hang_timeout" if timeout else "segv", suspect_case=n, location="src/ring_buffer.c:39", hypothesis="가설")
                for rest in names[i + 1:]:
                    results[rest] = CaseResult(name=rest, status="not_run")
                break
            m = re.search(r"FAIL ACTUAL=(\S+)", body)
            flaky = "FLAKY" in body and self._flaky.setdefault(n, 0) > 0
            if "FLAKY" in body:
                self._flaky[n] += 1
            broken = "USES_HELPER" in body and "BREAKS_OTHERS" in r.text
            if m or flaky or broken or ("FAILS_IN_FINAL" in body and final):
                actual = m.group(1) if m else "1"
                results[n] = CaseResult(name=n, status="fail", message=f"Expected equality\n  Which is: {actual}", expected="0", actual=actual, observed=[actual])
            else:
                results[n] = CaseResult(name=n, status="pass")
        return RunResult(r.function, results, 0 if crash is None else -11, 0.1, crash)

    def coverage(self, r: Rendered, cases: list[str], out_dir=None) -> FileCoverage | None:  # noqa: ANN001
        if self.executable_lines is None:
            return None
        self.coverage_calls.append(list(cases))
        bodies = case_bodies(r)
        hit = set(self.entry_lines)
        for n in cases:
            m = re.search(r"COVERS=([\d,]+)", bodies.get(n, ""))
            if m:
                hit |= {int(x) for x in m.group(1).split(",") if x}
        return FileCoverage(path=str(r.source), lines={ln: int(ln in hit) for ln in self.executable_lines})

    def install(self, r: Rendered) -> Path:
        p = self.install_root / r.file_name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(r.text, encoding="utf-8")
        self.installed[r.function] = r.text
        return p
