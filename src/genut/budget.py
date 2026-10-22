"""예산 관리 (설계 7.4, 7.7, 7.10).

빌드·시간 예산은 실행 단위(소스 파일 하나), 토큰·수정 횟수 예산은 함수 단위다.
빌드 예산 중 final_build_reserve는 최종 확인용으로 남겨 둔다.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from genut.config import BudgetConfig


class Budget:
    def __init__(self, cfg: BudgetConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self._clock = clock
        self._start = clock()
        self.builds_used = 0
        self.tokens: dict[str, int] = {}
        self.iterations = 0
        self.coverage_rounds = 0

    # ---------------------------------------------------------------- builds
    def builds_left(self, use_reserve: bool = False) -> int:
        reserve = 0 if use_reserve else self.cfg.final_build_reserve
        return max(0, self.cfg.max_builds_per_run - reserve - self.builds_used)

    def can_build(self, use_reserve: bool = False) -> bool:
        return self.builds_left(use_reserve) > 0 and (use_reserve or not self.time_exceeded())

    def record_build(self) -> None:
        self.builds_used += 1

    # ------------------------------------------------------------------ time
    def elapsed(self) -> float:
        return self._clock() - self._start

    def time_exceeded(self) -> bool:
        return self.elapsed() >= self.cfg.max_seconds_per_run

    # ---------------------------------------------------------------- tokens
    def add_tokens(self, function: str, n: int) -> None:
        self.tokens[function] = self.tokens.get(function, 0) + max(0, n)

    def tokens_exceeded(self, function: str) -> bool:
        return self.tokens.get(function, 0) >= self.cfg.max_tokens_per_function

    # ----------------------------------------------------------------- loops
    def can_iterate(self) -> bool:
        return self.iterations < self.cfg.max_iterations

    def can_fix(self, fixes_used: int) -> bool:
        return fixes_used < self.cfg.max_case_fixes

    def snapshot(self) -> dict:
        return {
            "builds_used": self.builds_used,
            "builds_max": self.cfg.max_builds_per_run,
            "final_reserve": self.cfg.final_build_reserve,
            "elapsed_sec": round(self.elapsed(), 1),
            "seconds_max": self.cfg.max_seconds_per_run,
            "iterations": self.iterations,
            "coverage_rounds": self.coverage_rounds,
            "tokens": dict(self.tokens),
        }

    def worst_case_minutes(self, seconds_per_build: float) -> float:
        return self.cfg.max_builds_per_run * seconds_per_build / 60.0
