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


class ClassInfo(BaseModel):
    """멤버 함수가 속한 클래스 (설계 6.3 C++ 요소, 13장 12단계). 테스트가 객체를 만들고 상태를 맞추는 데 쓴다."""

    name: str
    qualified_name: str
    kind: str = "class"  # class | struct
    file: str = ""
    line: int = 0
    constructors: list[str] = Field(default_factory=list)  # 공개 생성자
    default_constructible: bool = True
    abstract: bool = False
    template: bool = False
    fields: list[str] = Field(default_factory=list)  # "int value_ (private)"
    methods: list[str] = Field(default_factory=list)  # 공개 멤버 함수 (생성자·소멸자 제외)


class FunctionInfo(BaseModel):
    name: str
    key: str = ""  # 실행 안에서 함수를 가리키는 식별자. C 함수는 name, 멤버 함수는 Class_method, 오버로드는 _2, _3
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
    access: str = ""  # 멤버 함수의 public | protected | private
    is_const: bool = False
    is_constructor: bool = False
    owner: ClassInfo | None = None
    source: str = ""  # 함수 소스 전문
    calls: list[CallSite] = Field(default_factory=list)
    globals: list[GlobalRef] = Field(default_factory=list)
    returns: list[ReturnPoint] = Field(default_factory=list)
    branches: list[Branch] = Field(default_factory=list)
    types: list[TypeDef] = Field(default_factory=list)
    constants: dict[str, str] = Field(default_factory=dict)  # 열거형 상수·매크로 이름 -> 값/정의
    constant_files: dict[str, str] = Field(default_factory=dict)  # 상수 이름 -> 선언한 파일 (지식 노트 범위 판정, 설계 11.5)
    includes: list[str] = Field(default_factory=list)  # #include 줄
    preproc: list[str] = Field(default_factory=list)  # 함수가 놓인 #if 조건
    only_asm: bool = False
    empty_body: bool = False
    line_count: int = 0
    branch_count: int = 0

    @property
    def display(self) -> str:
        """사람과 LLM이 읽는 이름: 멤버 함수는 Class::method."""
        return f"{self.class_name}::{self.name}" if self.is_method and self.class_name else self.name

    @property
    def fixture_object(self) -> str:
        """gtest fixture가 기본 생성해 obj로 주는 클래스 (인스턴스 멤버 함수이고 기본 생성 가능할 때). 아니면 ""."""
        o = self.owner
        if not self.is_method or self.is_static_method or self.is_constructor or o is None or not o.default_constructible or o.abstract:
            return ""
        return o.qualified_name

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
