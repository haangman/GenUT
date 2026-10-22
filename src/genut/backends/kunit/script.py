"""KUnit 실행 스크립트 호출 (설계 12장 계약, docs/kunit-script.md).

    <script> <test_file> --mode build|run|all [--filter <suite>.<case>] --log <path> [--coverage-out <dir>]

종료 코드: 0 = 실행 완료(테스트 결과는 로그로 판단), 10 = 빌드 실패, 20 = 실행 실패(부팅 불가 등), 그 외 = 스크립트 오류.
전체 로그는 --log 파일에 쓰고 stdout에는 짧은 진행 메시지만 쓴다. 빌드 실패면 컴파일러 출력이 로그에 들어 있어야 한다.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from genut.backends.base import CommandResult, Runner, run_command

BUILD_FAILED, RUN_FAILED = 10, 20


@dataclass
class ScriptResult:
    mode: str
    returncode: int
    log: str  # --log 파일 내용 (없으면 stdout)
    log_path: Path
    seconds: float
    timed_out: bool = False
    coverage_dir: Path | None = None

    @property
    def built(self) -> bool:
        return self.returncode in (0, RUN_FAILED) and not self.timed_out

    @property
    def category(self) -> str:
        """0 ok | build_failed | run_failed | timeout | script_error"""
        if self.timed_out:
            return "timeout"
        return {0: "ok", BUILD_FAILED: "build_failed", RUN_FAILED: "run_failed"}.get(self.returncode, "script_error")


class KUnitScript:
    def __init__(self, script: Path, test_file: Path, timeout: float, runner: Runner = run_command, cwd: Path | None = None) -> None:
        self.script = script
        self.test_file = test_file
        self.timeout = timeout
        self.runner = runner
        self.cwd = cwd

    def command(self) -> list[str]:
        """Windows 개발 PC에서도 돌 수 있게 .py는 파이썬으로, .sh는 bash로 부른다."""
        s = str(self.script)
        if self.script.suffix == ".py":
            return [sys.executable, s]
        if self.script.suffix == ".sh" and os.name == "nt":
            return [shutil.which("bash") or "bash", s]
        return [s]

    def __call__(self, mode: str, log: Path, filter_: str = "", coverage_out: Path | None = None) -> ScriptResult:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.unlink(missing_ok=True)
        cmd = self.command() + [str(self.test_file), "--mode", mode, "--log", str(log)]
        if filter_:
            cmd += ["--filter", filter_]
        if coverage_out is not None:
            coverage_out.mkdir(parents=True, exist_ok=True)
            cmd += ["--coverage-out", str(coverage_out)]
        res: CommandResult = self.runner(cmd, cwd=self.cwd, timeout=self.timeout)
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else res.output
        return ScriptResult(mode, res.returncode, text, log, res.seconds, res.timed_out, coverage_out)
