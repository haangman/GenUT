"""공유 상태(블랙보드): 케이스, 공유 영역, 함수, 실행 (설계 4.1, 7.4, 7.10).

실행 상태는 빌드가 끝날 때마다 state.json에 저장한다. GenUT가 중간에 죽어도
그때까지 통과한 케이스가 남아 `genut finalize`로 마무리할 수 있다.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from genut.models import FailureInfo


class CaseStatus(StrEnum):
    NEW = "new"
    PASS = "pass"
    FAIL = "fail"
    CRASH = "crash"
    NOT_RUN = "not_run"
    DROPPED = "dropped"
    SUSPECTED_BUG = "suspected_bug"
    FLAKY = "flaky"


#: 최종 파일에 다시는 들어가지 않는 상태
TERMINAL = {CaseStatus.DROPPED, CaseStatus.SUSPECTED_BUG, CaseStatus.FLAKY}
#: 다음 빌드에 포함되는 상태
LIVE = {CaseStatus.NEW, CaseStatus.PASS, CaseStatus.FAIL, CaseStatus.CRASH, CaseStatus.NOT_RUN}


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class CaseEvent(BaseModel):
    round: int
    stage: str
    status: str
    note: str = ""


class TestCase(BaseModel):
    __test__ = False  # pytest가 테스트 클래스로 수집하지 않게

    name: str
    target_function: str
    purpose: str = ""
    body: str
    origin: str = "expand"  # smoke | expand | coverage | fix
    status: CaseStatus = CaseStatus.NEW
    fixes_used: int = 0
    review_fixes_used: int = 0
    last_failure: FailureInfo | None = None
    history: list[CaseEvent] = Field(default_factory=list)
    pass_body_hash: str | None = None
    pass_shared_version: int | None = None
    flaky_suspect: bool = False
    drop_reason: str = ""
    previous_body: str | None = None
    helpers_used: list[str] = Field(default_factory=list)
    covers_lines: list[int] = Field(default_factory=list)

    def body_hash(self) -> str:
        return text_hash(self.body)

    @property
    def is_live(self) -> bool:
        return self.status in LIVE

    def mark(self, status: CaseStatus, round_: int, stage: str, note: str = "") -> None:
        self.status = status
        self.history.append(CaseEvent(round=round_, stage=stage, status=status.value, note=note))

    def drop(self, round_: int, stage: str, reason: str, status: CaseStatus = CaseStatus.DROPPED) -> None:
        self.drop_reason = reason
        self.mark(status, round_, stage, reason)


class SharedArea(BaseModel):
    """여러 케이스가 함께 쓰는 부분 (설계 7.4 공유 영역)."""

    version: int = 0
    includes: list[str] = Field(default_factory=list)
    helpers: dict[str, str] = Field(default_factory=dict)
    setup_code: str = ""
    teardown_code: str = ""

    def digest(self) -> str:
        return text_hash(json.dumps(self.model_dump(exclude={"version"}), sort_keys=True))


class FunctionStatus(StrEnum):
    ACTIVE = "active"
    GIVEN_UP = "given_up"
    UNTESTABLE = "untestable"


class CoverageSummary(BaseModel):
    covered: int = 0
    total: int = 0
    uncovered_lines: list[int] = Field(default_factory=list)

    @property
    def percent(self) -> float:
        return 100.0 if self.total == 0 else 100.0 * self.covered / self.total


class UnreachableLine(BaseModel):
    line: int
    category: str  # unreachable_defensive | needs_stub_behavior | config_dependent | optimized_out | unknown
    reason: str = ""


class FunctionState(BaseModel):
    name: str
    status: FunctionStatus = FunctionStatus.ACTIVE
    status_reason: str = ""
    signature: str = ""
    start_line: int = 0
    end_line: int = 0
    cautions: list[str] = Field(default_factory=list)
    recommended: list[dict] = Field(default_factory=list)
    tokens_used: int = 0
    cases: list[TestCase] = Field(default_factory=list)
    shared: SharedArea = Field(default_factory=SharedArea)
    shared_history: list[SharedArea] = Field(default_factory=list)
    coverage: CoverageSummary | None = None
    unreachable: list[UnreachableLine] = Field(default_factory=list)
    narrowed: list[str] = Field(default_factory=list)
    review_notes: list[str] = Field(default_factory=list)
    result: str = ""  # complete | partial | none
    final_check: str = ""  # ok | partial | snapshot | unverified | none
    installed_path: str = ""

    def by_status(self, *statuses: CaseStatus) -> list[TestCase]:
        return [c for c in self.cases if c.status in statuses]

    def live_cases(self) -> list[TestCase]:
        return [c for c in self.cases if c.is_live]

    def passed(self) -> list[TestCase]:
        return self.by_status(CaseStatus.PASS)

    def case(self, name: str) -> TestCase | None:
        return next((c for c in self.cases if c.name == name), None)

    def unique_name(self, base: str) -> str:
        names = {c.name for c in self.cases}
        if base not in names:
            return base
        i = 2
        while f"{base}_{i}" in names:
            i += 1
        return f"{base}_{i}"

    def change_shared(self, new: SharedArea) -> None:
        """공유 영역을 바꾼다. 이전 판은 되돌리기용으로 보관한다."""
        self.shared_history.append(self.shared.model_copy(deep=True))
        new.version = self.shared.version + 1
        self.shared = new

    def rollback_shared(self) -> bool:
        if not self.shared_history:
            return False
        self.shared = self.shared_history.pop()
        return True


class Snapshot(BaseModel):
    """녹색 스냅샷: 포함된 케이스가 모두 통과한 빌드 (설계 7.10)."""

    build_no: int
    function: str
    path: str
    cases: list[str]


class RunState(BaseModel):
    run_id: str
    source_file: str
    backend: str
    product: str = ""
    functions: dict[str, FunctionState] = Field(default_factory=dict)
    round: int = 0
    builds_used: int = 0
    seconds_used: float = 0.0
    stage: str = "init"
    snapshots: list[Snapshot] = Field(default_factory=list)
    finished: bool = False
    interrupted: bool = False
    stub_includes: list[str] = Field(default_factory=list)
    stub_files: list[str] = Field(default_factory=list)
    stub_symbols: list[str] = Field(default_factory=list)
    preflight_seconds: float | None = None
    smoke_skipped: bool = False
    notes: list[str] = Field(default_factory=list)

    def all_cases(self) -> list[TestCase]:
        return [c for f in self.functions.values() for c in f.cases]

    def active_functions(self) -> list[FunctionState]:
        return [f for f in self.functions.values() if f.status == FunctionStatus.ACTIVE]

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> RunState:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
