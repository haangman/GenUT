"""프롬프트 조립기: 컨텍스트 한도 관리 (설계 9.7).

에이전트는 프롬프트 문자열 대신 조각 목록을 낸다. 조각마다 우선순위가 있다.
- 1 필수: 줄이지 않는다 (역할 지시문, 규칙 md, 출력 형식, 대상 시그니처·타입)
- 2 축약 가능: shrinkers가 차례로 더 작은 판을 돌려준다 (빈 문자열이면 사실상 제거)
- 3 생략 가능: 넘치면 통째로 뺀다
허용량 = max_context × (1 − 안전 여유) − 출력 예약. 넘치면 3번을 뒤에서부터 하나씩 빼고,
그래도 넘치면 2번을 뒤에서부터 한 단계씩 줄이며 매번 다시 잰다. 1번만으로도 넘치면 ContextOverflow.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from genut.llm.tokens import TokenCounter

REQUIRED, SHRINKABLE, OPTIONAL = 1, 2, 3


@dataclass
class Fragment:
    name: str
    text: str
    priority: int = REQUIRED
    target: str = "user"  # system | user
    shrinkers: list[Callable[[], str]] = field(default_factory=list)
    title: str = ""  # 비어 있지 않으면 "## {title}" 머리말을 붙인다

    def render(self) -> str:
        if not self.text:
            return ""
        return f"## {self.title}\n\n{self.text}" if self.title else self.text


@dataclass
class Trim:
    name: str
    action: str  # dropped | shrunk
    before: int
    after: int


@dataclass
class BuiltPrompt:
    system: str
    user: str
    tokens: int
    limit: int
    trims: list[Trim] = field(default_factory=list)

    def trim_summary(self) -> list[dict]:
        return [t.__dict__ for t in self.trims]


class ContextOverflow(Exception):
    def __init__(self, tokens: int, limit: int, required: list[tuple[str, int]]) -> None:
        names = ", ".join(f"{n}({t})" for n, t in sorted(required, key=lambda x: -x[1])[:5])
        super().__init__(f"필수 조각만으로 {tokens} 토큰이 되어 허용량 {limit}을 넘습니다. 큰 조각: {names}")
        self.tokens = tokens
        self.limit = limit
        self.required = required


def compute_limit(max_context: int, output_reserve: int, safety_margin: float) -> int:
    return max(0, int(max_context * (1.0 - safety_margin)) - output_reserve)


class PromptBuilder:
    def __init__(self, counter: TokenCounter, max_context: int, output_reserve: int, safety_margin: float = 0.05) -> None:
        self.counter = counter
        self.limit = compute_limit(max_context, output_reserve, safety_margin)

    def _join(self, frags: list[Fragment], target: str) -> str:
        return "\n\n".join(r for f in frags if f.target == target and (r := f.render()))

    def _count(self, frags: list[Fragment]) -> int:
        return self.counter(self._join(frags, "system")) + self.counter(self._join(frags, "user"))

    def build(self, fragments: list[Fragment]) -> BuiltPrompt:
        frags = [Fragment(f.name, f.text, f.priority, f.target, list(f.shrinkers), f.title) for f in fragments]
        trims: list[Trim] = []
        total = self._count(frags)
        # 3: 뒤에서부터 통째로 뺀다
        for f in reversed([f for f in frags if f.priority == OPTIONAL]):
            if total <= self.limit:
                break
            before = self.counter(f.render())
            f.text = ""
            trims.append(Trim(f.name, "dropped", before, 0))
            total = self._count(frags)
        # 2: 뒤에서부터 한 단계씩 줄인다
        while total > self.limit:
            cand = [f for f in frags if f.priority == SHRINKABLE and f.shrinkers and f.text]
            if not cand:
                break
            f = cand[-1]
            before = self.counter(f.render())
            f.text = f.shrinkers.pop(0)()
            after = self.counter(f.render())
            trims.append(Trim(f.name, "shrunk" if f.text else "dropped", before, after))
            total = self._count(frags)
        if total > self.limit:
            req = [(f.name, self.counter(f.render())) for f in frags if f.text]
            raise ContextOverflow(total, self.limit, req)
        return BuiltPrompt(self._join(frags, "system"), self._join(frags, "user"), total, self.limit, trims)
