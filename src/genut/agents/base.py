"""에이전트 공통 틀 (설계 4.3, 9.4, 9.7).

호출 하나는 항상 같은 길을 간다.
1. 함수당 토큰 예산을 다 썼으면 부르지 않는다 (budget)
2. 조각 목록을 프롬프트 조립기로 한도 안에 맞춘다. 필수 조각만으로 넘치면 부르지 않는다 (context_overflow)
3. 구조화 출력 계층으로 pydantic 모델을 받는다
4. 프롬프트·응답 전문과 잘린 조각을 실행 기록에 남기고, 쓴 토큰을 예산에 더한다
시스템 프롬프트 = 역할 지시문 + 규칙 md (+ 스텁 도구 md), 유저 프롬프트 = 이번 호출의 입력 + 할 일.
역할 지시문은 $GENUT_HOME/prompts/<role>.md가 있으면 그것을, 없으면 패키지 기본값(prompt_language)을 쓴다.
"""

from __future__ import annotations

import json
import re
import string
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel

from genut.budget import Budget
from genut.config import GenutConfig
from genut.llm.prompt_builder import REQUIRED, SHRINKABLE, ContextOverflow, Fragment, PromptBuilder, Trim
from genut.llm.provider import ChatProvider, Message, ProviderError, Usage
from genut.llm.structured import FencedSpec, StructuredError, ask_structured, format_instructions, strict_schema
from genut.llm.tokens import TokenCounter
from genut.rules import Rules, strip_comments
from genut.state import text_hash
from genut.trace import Trace

T = TypeVar("T", bound=BaseModel)

_SECTION = re.compile(r"^##[ \t]+(\S+)[ \t]*$", re.M)
_CORE = re.compile(r"<!--\s*genut:core\s*-->(.*?)(?:<!--\s*/genut:core\s*-->|\Z)", re.S)
REPAIR_MESSAGE_TOKENS = 100  # 복구 요청 문구 몫

FRAMEWORK = {"gtest": "GoogleTest (gtest)", "kunit": "KUnit"}
LANGUAGE = {"gtest": "C++", "kunit": "C"}
WRAPPER = {"gtest": "TEST(<suite>, <case name>) { ... }", "kunit": "static void <case name>(struct kunit *test) { ... }"}


# ------------------------------------------------------------------ prompts
@dataclass
class PromptTemplate:
    """역할 프롬프트 파일. 첫 '## ' 제목 앞은 역할 지시문, '## <이름>' 절은 단계별 지시·예시.

    본문에는 $function 같은 자리표시자를 쓸 수 있다 (string.Template).
    """

    preamble: str
    sections: dict[str, str]
    sources: list[str]

    @classmethod
    def parse(cls, text: str, source: str) -> PromptTemplate:
        parts = _SECTION.split(strip_comments(text))
        sections = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
        return cls(parts[0].strip(), sections, [source])

    def over(self, base: PromptTemplate) -> PromptTemplate:
        """사용자 파일(self)을 패키지 기본값(base) 위에 겹친다. 사용자 파일에 없는 절은 기본값을 쓴다."""
        return PromptTemplate(self.preamble or base.preamble, {**base.sections, **self.sections}, self.sources + base.sources)

    def render(self, section: str | None, values: dict[str, str]) -> str:
        text = self.preamble if section is None else self.sections.get(section, "")
        return string.Template(text).safe_substitute(values)

    def digest(self) -> str:
        return text_hash(json.dumps([self.preamble, self.sections], ensure_ascii=False, sort_keys=True))


def load_prompt(role: str, language: str, home: Path | None = None) -> PromptTemplate:
    pkg = resources.files("genut.agents") / "prompts" / language / f"{role}.md"
    base = PromptTemplate.parse(pkg.read_text(encoding="utf-8"), f"genut:prompts/{language}/{role}.md")
    user = home / "prompts" / f"{role}.md" if home else None
    if user is not None and user.exists():
        return PromptTemplate.parse(user.read_text(encoding="utf-8"), str(user)).over(base)
    return base


def core_section(md: str) -> str:
    """스텁 도구 md에서 <!-- genut:core --> 구간만 (설계 9.7). 표시가 없으면 빈 문자열."""
    return "\n\n".join(m.strip() for m in _CORE.findall(md))


def fit(fragments: list[Fragment], budget: int, counter: TokenCounter) -> list[Fragment]:
    """조각 묶음 하나를 자기 토큰 상한(분석 보고서 3,000 등)에 맞춘다. 필수 조각은 줄이지 않는다."""
    frags = [Fragment(f.name, f.text, f.priority, f.target, list(f.shrinkers), f.title) for f in fragments]

    def total() -> int:
        return sum(counter(f.render()) for f in frags)

    while total() > budget:
        cand = [f for f in frags if f.priority == SHRINKABLE and f.shrinkers and f.text]
        if not cand:
            break
        cand[-1].text = cand[-1].shrinkers.pop(0)()
    return frags


# -------------------------------------------------------------------- agent
@dataclass
class AgentEnv:
    """에이전트들이 함께 쓰는 것. 실행 하나에 하나."""

    provider: ChatProvider
    counter: TokenCounter
    config: GenutConfig
    rules: Rules
    home: Path | None = None
    trace: Trace | None = None
    budget: Budget | None = None


class AgentError(Exception):
    """에이전트 호출 실패. kind: budget | context_overflow | invalid | refusal | length | infra_error"""

    def __init__(self, role: str, kind: str, message: str, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.role = role
        self.kind = kind
        self.usage = usage or Usage()


@dataclass
class AgentResult(Generic[T]):
    value: T
    usage: Usage
    attempts: int
    path: str  # native | parse | fenced
    tokens: int  # 보내기 전에 잰 프롬프트 토큰
    limit: int
    trims: list[Trim] = field(default_factory=list)
    prompt_file: Path | None = None


class Agent(Generic[T]):
    role: ClassVar[str] = ""
    output: ClassVar[type[BaseModel]]
    fenced: ClassVar[FencedSpec | None] = None

    def __init__(self, env: AgentEnv) -> None:
        self.env = env
        self.prompt = load_prompt(self.role, env.config.llm.prompt_language, env.home)

    # ---------------------------------------------------------- fragments
    def values(self, function: str, **extra: str) -> dict[str, str]:
        b = self.env.config.backend
        return {"function": function, "backend": b, "framework": FRAMEWORK[b], "language": LANGUAGE[b], "wrapper": WRAPPER[b], **extra}

    def base_fragments(self, values: dict[str, str]) -> list[Fragment]:
        frags = [Fragment("role", self.prompt.render(None, values), REQUIRED, target="system")]
        rules = self.env.rules.prompt_block()
        if rules:
            frags.append(Fragment("rules", rules, REQUIRED, target="system"))
        return frags

    def task_fragment(self, section: str, values: dict[str, str]) -> Fragment:
        return Fragment("task", self.prompt.render(section, values), REQUIRED, title="할 일")

    # --------------------------------------------------------------- call
    def call(self, fragments: list[Fragment], *, function: str, label: str = "") -> AgentResult[T]:
        env, llm = self.env, self.env.config.llm
        label = label or self.role
        if env.budget is not None and env.budget.tokens_exceeded(function):
            self._event("agent_skipped", function, label=label, reason="budget")
            raise AgentError(self.role, "budget", f"{function}: 함수당 토큰 예산을 다 썼습니다.")

        caps = env.provider.capabilities
        use_fenced = llm.code_format == "fenced" and self.fenced is not None
        reserve = llm.context.output_reserve_tokens
        fmt = 0  # 스키마 제약이 없으면 구조화 출력 계층이 출력 형식 안내를 덧붙이므로 그만큼 비워 둔다
        if use_fenced or not caps.json_schema:
            fenced = self.fenced if use_fenced else None
            schema = strict_schema(self.output, {fenced.exclude_path()} if fenced else None)
            fmt = env.counter(format_instructions(schema, fenced))
        builder = PromptBuilder(env.counter, caps.max_context, reserve + fmt, llm.context.context_safety_margin)
        try:
            built = builder.build(fragments)
        except ContextOverflow as e:
            self._event("context_overflow", function, label=label, tokens=e.tokens, limit=e.limit, required=e.required)
            raise AgentError(self.role, "context_overflow", str(e)) from e

        # 복구 요청은 직전 응답을 입력에 덧붙인다. 한도 안에 들어갈 만큼만 복구를 허용한다.
        max_repair = max(0, min(2, (built.limit - built.tokens) // (reserve + REPAIR_MESSAGE_TOKENS)))
        meta: dict[str, Any] = {
            "agent": self.role,
            "function": function,
            "tokens": built.tokens,
            "limit": built.limit,
            "trims": built.trim_summary(),
            "prompt": self.prompt.sources[0],
            "prompt_hash": self.prompt.digest(),
        }
        messages = [Message("system", built.system), Message("user", built.user)]
        try:
            res = ask_structured(
                env.provider, messages, self.output, tag=self.role, temperature=llm.temperature, max_tokens=reserve,
                max_repair=max_repair, code_format=llm.code_format, fenced=self.fenced,
            )
        except StructuredError as e:
            usage = e.result.usage if e.result else Usage()
            self._spend(function, usage)
            text = e.result.text if e.result else ""
            attempts = e.result.attempts if e.result else 1
            self._record(label, built.system, built.user, text, None, {**meta, "error": e.kind, "attempts": attempts, "usage": usage.__dict__})
            raise AgentError(self.role, e.kind, str(e), usage) from e
        except ProviderError as e:
            self._event("infra_error", function, label=label, error=str(e))
            raise AgentError(self.role, "infra_error", str(e)) from e

        self._spend(function, res.usage)
        meta.update(attempts=res.attempts, path=res.path, usage=res.usage.__dict__, model=env.provider.model)
        pfile = self._record(label, built.system, built.user, res.text, res.value.model_dump(), meta)
        return AgentResult(res.value, res.usage, res.attempts, res.path, built.tokens, built.limit, built.trims, pfile)  # type: ignore[arg-type]

    # ------------------------------------------------------------ helpers
    def _spend(self, function: str, usage: Usage) -> None:
        if self.env.budget is not None:
            self.env.budget.add_tokens(function, usage.total)

    def _record(self, label: str, system: str, user: str, response: str, parsed: Any, meta: dict[str, Any]) -> Path | None:
        if self.env.trace is None:
            return None
        return self.env.trace.prompt(label, system, user, response, parsed, meta)

    def _event(self, type_: str, function: str, **data: Any) -> None:
        if self.env.trace is not None:
            self.env.trace.event(type_, agent=self.role, function=function, **data)
