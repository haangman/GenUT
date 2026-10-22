"""스텁 도구 연동 지점 (설계 10.2, 10.4).

백엔드별 스텁 도구 어댑터는 스텁 도구 md가 들어온 뒤 만든다(13장 11단계). 그때까지는
NoopStubAdapter가 동작한다. 스텁을 만들지 못하므로 스텁이 필요한 심볼은 unsupported로 돌려주고,
그 심볼을 거치는 경로는 오케스트레이터가 narrow_scope 또는 테스트 불가로 처리한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from genut.analysis.model import FunctionInfo, SymbolJudgment


class StubSpec(BaseModel):
    name: str
    behavior: str = ""  # 기본 동작 (예: "0을 돌려준다")


class StubPlan(BaseModel):
    symbols: list[StubSpec] = Field(default_factory=list)
    unsupported: list[str] = Field(default_factory=list)  # 스텁이 필요하지만 만들 수 없는 심볼
    notes: list[str] = Field(default_factory=list)


class StubArtifacts(BaseModel):
    files: list[str] = Field(default_factory=list)  # 테스트 빌드에 더할 소스
    includes: list[str] = Field(default_factory=list)  # 테스트 파일에 넣을 #include 줄
    link_flags: list[str] = Field(default_factory=list)
    usage_snippet: str = ""  # 테스트에서 스텁 반환값을 정하는 예 (Test Writer 프롬프트용)


class StubAdapter(Protocol):
    name: str

    def plan_stubs(self, fn: FunctionInfo, deps: list[SymbolJudgment], must_stub: list[str]) -> StubPlan: ...

    def generate(self, plan: StubPlan, out_dir: Path) -> StubArtifacts: ...

    def describe(self) -> str: ...


NOOP_DESCRIPTION = (
    "스텁 도구가 아직 연동되지 않았습니다. 정의가 없는 심볼은 스텁할 수 없으므로, "
    "그런 심볼을 호출하는 경로는 테스트하지 말고 그 심볼에 닿기 전에 끝나는 경로(인자 검사 등)만 다루세요."
)


class NoopStubAdapter:
    name = "noop"

    def plan_stubs(self, fn: FunctionInfo, deps: list[SymbolJudgment], must_stub: list[str]) -> StubPlan:
        need = sorted({d.name for d in deps if d.needs_stub} | {m for m in must_stub if any(d.name == m for d in deps)})
        notes = [f"{n}: 스텁 도구 연동 필요" for n in need]
        if fn.is_static:
            notes.append(f"{fn.name}: static 함수라 스텁 도구의 static 접근이 필요합니다 (스텁 도구 연동 필요).")
        return StubPlan(unsupported=need, notes=notes)

    def generate(self, plan: StubPlan, out_dir: Path) -> StubArtifacts:
        return StubArtifacts()

    def describe(self) -> str:
        return NOOP_DESCRIPTION
