"""여러 계층이 함께 쓰는 데이터 모델 (순환 import를 피하려고 한곳에 둔다)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Diagnostic(BaseModel):
    """컴파일러·의미 검사가 낸 진단 하나 (설계 7.3)."""

    file: str = ""
    line: int = 0
    col: int = 0
    severity: str = "error"  # error | warning | note | fatal
    message: str = ""
    flag: str = ""  # [-Werror=unused-variable] 등의 경고 플래그
    notes: list[str] = Field(default_factory=list)
    snippet: str = ""
    stage: str = "compile"  # check | compile | link
    owner: str = ""  # 이 진단이 귀속된 케이스 이름. 공유 영역이면 "@shared", 모르면 ""

    def short(self) -> str:
        loc = f"{self.file}:{self.line}:{self.col}" if self.file else "(위치 없음)"
        flag = f" [{self.flag}]" if self.flag else ""
        return f"{loc}: {self.severity}: {self.message}{flag}"


class CrashBrief(BaseModel):
    """원시 oops/새니타이저 리포트 대신 LLM에 주는 요약 (설계 8.3)."""

    category: str = "unknown"
    location: str = ""  # file:line
    source_line: str = ""
    chain: list[str] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)
    hypothesis: str = ""
    suspect_case: str = ""
    raw_excerpt: str = ""

    def render(self) -> str:
        lines = ["## 크래시 브리프", f"분류: {self.category}"]
        if self.location:
            loc = f"위치: {self.location}"
            if self.source_line:
                loc += f"  `{self.source_line.strip()}`"
            lines.append(loc + "   <- 책임 프레임")
        if self.chain:
            lines.append("호출 체인 (대상/테스트 프레임만):")
            lines += [f"  {'-> ' if i else ''}{c}" for i, c in enumerate(self.chain)]
        if self.related:
            lines.append("관련 심볼: " + ", ".join(self.related))
        if self.suspect_case:
            lines.append(f"용의 케이스: {self.suspect_case}")
        if self.hypothesis:
            lines.append(f"가설: {self.hypothesis}")
        return "\n".join(lines)


class FailureInfo(BaseModel):
    """케이스 하나의 실패 정보. 분류 결과와 LLM에 줄 축약 근거를 담는다."""

    category: str
    stage: str  # check | compile | link | run | review | final
    summary: str = ""
    details: str = ""
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    expected: str | None = None
    actual: str | None = None
    observed: list[str] = Field(default_factory=list)  # 실패 메시지에 나온 값들 (실제값 베끼기 검출용)
    crash: CrashBrief | None = None
    recommended_strategy: str = "fix_case"
    review_check: str | None = None


class CaseResult(BaseModel):
    """실행 한 번에서 케이스 하나의 결과."""

    name: str
    status: str  # pass | fail | crash | not_run | timeout
    message: str = ""
    expected: str | None = None
    actual: str | None = None
    observed: list[str] = Field(default_factory=list)
    details: str = ""
    file: str = ""
    line: int = 0
