"""Anthropic Messages API provider (개발용, 설계 9.1).

공식 anthropic SDK를 쓴다. 참고 사항:
- 2026-10-21부터 Messages API는 temperature/top_p/top_k를 받지 않으므로 보내지 않는다.
- 구조화 출력은 output_config.format(json_schema)로 요청한다.
- 거절(stop_reason == "refusal")에 대비해 기본으로 서버 측 fallbacks="default"를 켠다.
- 429/5xx/연결 오류 재시도는 SDK가 max_retries만큼 한다.
"""

from __future__ import annotations

import os
from typing import Any

from genut.config import AnthropicConfig
from genut.llm.provider import Capabilities, Message, ProviderError, Response, TransportError, Usage, split_system

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, cfg: AnthropicConfig, model: str, max_retries: int = 4, client: Any = None) -> None:
        self.cfg = cfg
        self.model = model
        self.capabilities = Capabilities(
            native_tools=True, json_schema=True, max_context=cfg.max_context, supports_n=False, supports_temperature=False
        )
        if client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover - 설치 문제
                raise ProviderError("anthropic SDK가 없습니다: pip install 'genut[claude]'") from e
            kwargs: dict[str, Any] = {"max_retries": max_retries, "timeout": cfg.timeout_sec}
            key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
            if key:
                kwargs["api_key"] = key
            if cfg.base_url:
                kwargs["base_url"] = cfg.base_url
            client = anthropic.Anthropic(**kwargs)
        self.client = client

    def _params(self, messages: list[Message], response_schema: dict | None, max_tokens: int) -> dict[str, Any]:
        system, rest = split_system(messages)
        output_config: dict[str, Any] = {}
        if self.cfg.effort:
            output_config["effort"] = self.cfg.effort
        if response_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": response_schema}
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max(max_tokens, self.cfg.max_output_tokens),
            "messages": [{"role": m.role, "content": m.content} for m in rest],
        }
        if system:
            params["system"] = system
        if output_config:
            params["output_config"] = output_config
        return params

    def chat(
        self,
        messages: list[Message],
        *,
        response_schema: dict[str, Any] | None = None,
        schema_name: str = "output",
        temperature: float | None = None,  # 무시 (API가 받지 않음)
        max_tokens: int = 6000,
        tag: str = "",
    ) -> Response:
        import anthropic

        params = self._params(messages, response_schema, max_tokens)
        try:
            if self.cfg.fallbacks:
                resp = self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks=self.cfg.fallbacks, **params)
            else:
                resp = self.client.messages.create(**params)
        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError) as e:
            raise TransportError(f"Anthropic API 일시 오류(재시도 소진): {e}") from e
        except anthropic.APIStatusError as e:
            if getattr(e, "status_code", 0) >= 500:
                raise TransportError(f"Anthropic API 서버 오류: {e}") from e
            raise ProviderError(f"Anthropic API 요청 오류: {e}") from e

        stop = getattr(resp, "stop_reason", "end_turn")
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        finish = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "refusal": "refusal"}.get(stop, "stop")
        usage = getattr(resp, "usage", None)
        return Response(
            text=text,
            finish_reason=finish,
            usage=Usage(getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0),
            model=getattr(resp, "model", self.model),
            raw=resp,
        )

    def count_tokens(self, messages: list[Message]) -> int | None:
        system, rest = split_system(messages)
        kwargs: dict[str, Any] = {"model": self.model, "messages": [{"role": m.role, "content": m.content} for m in rest] or [{"role": "user", "content": "."}]}
        if system:
            kwargs["system"] = system
        try:
            return int(self.client.messages.count_tokens(**kwargs).input_tokens)
        except Exception:  # noqa: BLE001 - 세기 실패는 추정으로 대신
            return None
