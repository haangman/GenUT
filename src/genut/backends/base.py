"""백엔드 공통 입력·결과 형식 (설계 3.2 ③, 7.2, 7.8). gtest와 KUnit이 같은 모양으로 주고받는다.

빌드는 소스 파일 단위로 묶어 한 번, 실행 결과와 진단은 함수(테스트 파일)별로 나눠 돌려준다.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from genut.analysis.compile_db import CompileDB
from genut.analysis.model import AnalysisReport
from genut.backends.stub import StubArtifacts
from genut.coverage import FileCoverage
from genut.models import CaseResult, CrashBrief, Diagnostic
from genut.state import LIVE, FunctionState, TestCase
from genut.verify.diagnostics import LineMap
from genut.verify.staged import CheckResult


@dataclass
class TestUnit:
    """함수 하나의 테스트 파일을 만드는 데 필요한 것."""

    __test__ = False

    source: Path  # 대상 소스 (절대 경로)
    report: AnalysisReport
    state: FunctionState
    stub: StubArtifacts = field(default_factory=StubArtifacts)

    @property
    def function(self) -> str:
        return self.state.name

    def live_cases(self) -> list[TestCase]:
        return [c for c in self.state.cases if c.status in LIVE]


@dataclass
class Rendered:
    """렌더링한 테스트 파일 하나."""

    function: str
    file_name: str
    text: str
    line_map: LineMap
    cases: list[str]
    suite: str = ""
    source: Path | None = None  # 대상 소스


@dataclass
class CommandResult:
    cmd: list[str]
    returncode: int
    output: str
    seconds: float
    timed_out: bool = False


Runner = Callable[..., CommandResult]


def run_command(cmd: list[str], cwd: Path | None = None, timeout: float | None = None, env: dict[str, str] | None = None) -> CommandResult:
    """명령을 실행하고 stdout+stderr를 합쳐 돌려준다. 시간을 넘기면 죽이고 timed_out으로 표시한다."""
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, timeout=timeout)
        out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
        return CommandResult(cmd, p.returncode, out.replace("\r\n", "\n"), time.monotonic() - t0)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") + (e.stderr or b"").decode("utf-8", "replace")
        return CommandResult(cmd, -1, out.replace("\r\n", "\n"), time.monotonic() - t0, timed_out=True)
    except OSError as e:
        return CommandResult(cmd, -1, f"{cmd[0]} 실행 실패: {e}", time.monotonic() - t0)


@dataclass
class PreflightResult:
    ok: bool
    seconds: float
    log: str = ""
    log_path: Path | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    category: str = ""  # 실패면 infra_error | header_not_cpp_clean
    hint: str = ""


@dataclass
class BuildResult:
    ok_functions: list[str]
    failed_functions: list[str]
    diagnostics: dict[str, list[Diagnostic]]  # 함수 -> owner가 채워진 진단
    seconds: float
    log_path: Path | None = None
    unattributed: list[Diagnostic] = field(default_factory=list)  # 어느 함수 것인지 모르는 진단


class Backend(Protocol):
    """오케스트레이터가 쓰는 백엔드 인터페이스. gtest와 KUnit이 구현한다."""

    name: str

    def configure(self, units: list[TestUnit]) -> CommandResult: ...

    def compile_db(self) -> CompileDB | None: ...

    def link_context(self, unit: TestUnit) -> object: ...

    def fingerprint(self) -> str: ...

    def render(self, unit: TestUnit, cases: list[TestCase] | None = None) -> Rendered: ...

    def preflight(self, units: list[TestUnit], out_dir: Path | None = None) -> PreflightResult: ...

    def check(self, r: Rendered) -> CheckResult: ...

    def build(self, rendered: list[Rendered], out_dir: Path | None = None) -> BuildResult: ...

    def run(self, r: Rendered, out_dir: Path | None = None, only: list[str] | None = None) -> RunResult: ...

    def coverage(self, r: Rendered, cases: list[str], out_dir: Path | None = None) -> FileCoverage | None: ...

    def install(self, r: Rendered) -> Path: ...


@dataclass
class RunResult:
    function: str
    results: dict[str, CaseResult]  # 케이스 이름 -> 결과. 실행되지 못한 케이스는 not_run
    returncode: int
    seconds: float
    crash: CrashBrief | None = None
    timed_out: bool = False
    log_path: Path | None = None
