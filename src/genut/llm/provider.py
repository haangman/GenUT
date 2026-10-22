"""ChatProvider 인터페이스 (설계 9.3).

에이전트는 이 인터페이스만 알고, 뒤에 있는 모델이 Claude인지 Gemma 4인지 모른다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Capabilities:
    native_tools: bool
    json_schema: bool  # JSON 스키마 제약 출력 지원
    max_context: int  # 토큰
    supports_n: bool = False  # 한 요청에 n개 샘플
    supports_temperature: bool = True


@dataclass
class Message:
    role: str  # system | user | assistant
    content: str


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens)


@dataclass
class Response:
    text: str
    finish_reason: str = "stop"  # stop | length | refusal
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    raw: Any = None


class ProviderError(Exception):
    """요청 자체가 잘못된 경우 (재시도해도 같음)."""


class TransportError(ProviderError):
    """429/5xx/타임아웃/연결 끊김이 재시도 후에도 계속된 경우. infra_error로 실행을 멈춘다."""


class ChatProvider(Protocol):
    name: str
    model: str
    capabilities: Capabilities

    def chat(
        self,
        messages: list[Message],
        *,
        response_schema: dict[str, Any] | None = None,
        schema_name: str = "output",
        temperature: float | None = None,
        max_tokens: int = 6000,
        tag: str = "",
    ) -> Response: ...

    def count_tokens(self, messages: list[Message]) -> int | None:
        """정확한 토큰 수. 서버가 제공하지 않으면 None (추정으로 대신)."""
        ...


def split_system(messages: list[Message]) -> tuple[str, list[Message]]:
    system = "\n\n".join(m.content for m in messages if m.role == "system")
    return system, [m for m in messages if m.role != "system"]
