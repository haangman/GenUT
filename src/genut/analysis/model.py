"""정적 분석 결과 모델 (설계 6.3)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ParamInfo(BaseModel):
    name: str
    type: str
    canonical: str = ""
    record: str = ""  # 포인터든 값이든 구조체면 그 이름 (struct rb)


class TypeDef(BaseModel):
    name: str
    kind: str  # struct | union | enum | typedef | class
    text: str  # 정의 소스 (필드까지)
    file: str = ""
    line: int = 0
    fields: list[str] = Field(default_factory=list)
    example_init: str = ""


class CallSite(BaseModel):
    name: str
    line: int
    decl_file: str = ""
    decl_line: int = 0
    def_file: str = ""  # 이 번역 단위에서 정의를 찾은 파일 (없으면 "")
    is_static: bool = False
    is_inline: bool = False
    indirect: bool = False  # 함수 포인터 호출
    signature: str = ""


class GlobalRef(BaseModel):
    name: str
    type: str = ""
    is_static: bool = False
    access: str = "read"  # read | write | rw
    decl_file: str = ""


class ReturnPoint(BaseModel):
    line: int
    expr: str
    condition: str = ""  # 가장 가까운 분기 조건 (방향 포함)


class Branch(BaseModel):
    kind: str  # if | else | loop | case | default | ternary
    line: int
    condition: str
    start: int  # 이 분기 몸체의 시작 줄
    end: int


class FunctionInfo(BaseModel):
    name: str
    qualified_name: str = ""
    signature: str
    return_type: str
    params: list[ParamInfo] = Field(default_factory=list)
    file: str
    start_line: int
    end_line: int
    body_start: int = 0
    is_static: bool = False
    is_inline: bool = False
    is_method: bool = False
    is_static_method: bool = False
    is_template: bool = False
    class_name: str = ""
    namespace: str = ""
    source: str = ""  # 함수 소스 전문
    calls: list[CallSite] = Field(default_factory=list)
    globals: list[GlobalRef] = Field(default_factory=list)
    returns: list[ReturnPoint] = Field(default_factory=list)
    branches: list[Branch] = Field(default_factory=list)
    types: list[TypeDef] = Field(default_factory=list)
    constants: dict[str, str] = Field(default_factory=dict)  # 열거형 상수·매크로 이름 -> 값/정의
    includes: list[str] = Field(default_factory=list)  # #include 줄
    preproc: list[str] = Field(default_factory=list)  # 함수가 놓인 #if 조건
    only_asm: bool = False
    empty_body: bool = False
    line_count: int = 0
    branch_count: int = 0

    def reach(self, line: int) -> Branch | None:
        """그 줄을 감싸는 가장 안쪽 분기."""
        inner = [b for b in self.branches if b.start <= line <= b.end]
        return min(inner, key=lambda b: b.end - b.start) if inner else None


class Scope(StrEnum):
    SAME_TU = "same_tu"
    LINKABLE = "linkable"
    EXTERNAL = "external"
    UNDEFINED = "undefined"
    MUST_STUB = "must_stub"


class SymbolJudgment(BaseModel):
    name: str
    scope: Scope
    reason: str = ""
    evidence: str = ""
    is_static: bool = False

    @property
    def needs_stub(self) -> bool:
        return self.scope in (Scope.UNDEFINED, Scope.MUST_STUB)


class AnalysisReport(BaseModel):
    function: FunctionInfo
    linkscope: list[SymbolJudgment] = Field(default_factory=list)
    compile_args: list[str] = Field(default_factory=list)
    parse_errors: list[str] = Field(default_factory=list)
    dropped_flags: list[str] = Field(default_factory=list)

    def stub_required(self) -> list[str]:
        return [j.name for j in self.linkscope if j.needs_stub]
