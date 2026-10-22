"""OpenAI 호환 엔드포인트 provider (운영용 Gemma 4, 설계 9.1).

POST {base_url}/chat/completions 만 쓴다. 서버가 /tokenize를 주면(vLLM 등) 정확한 토큰 수에 쓴다.
전송 오류(429/5xx/타임아웃/연결)는 지수 백오프(1, 2, 4, 8초)로 max_retries회까지 재시도한다.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from genut.config import OpenAICompatConfig
from genut.llm.provider import Capabilities, Message, ProviderError, Response, TransportError, Usage

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(
        self,
        cfg: OpenAICompatConfig,
        max_retries: int = 4,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self.model = cfg.model
        self.max_retries = max_retries
        self._sleep = sleep
        self.capabilities = Capabilities(
            native_tools=True, json_schema=cfg.supports_json_schema, max_context=cfg.max_context, supports_n=cfg.supports_n
        )
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self.client = client or httpx.Client(base_url=cfg.base_url.rstrip("/"), headers=headers, timeout=cfg.timeout_sec)
        self._tokenize_ok: bool | None = None

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        delay = 1.0
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                r = self.client.post(path, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = e
            else:
                if r.status_code < 400:
                    return r.json()
                if r.status_code not in RETRY_STATUS:
                    raise ProviderError(f"LLM 요청 오류 {r.status_code}: {r.text[:500]}")
                last = ProviderError(f"HTTP {r.status_code}: {r.text[:200]}")
            if attempt < self.max_retries:
                self._sleep(delay)
                delay = min(delay * 2, 8.0)
        raise TransportError(f"LLM 엔드포인트 일시 오류(재시도 {self.max_retries}회 소진): {last}")

    def chat(
        self,
        messages: list[Message],
        *,
        response_schema: dict[str, Any] | None = None,
        schema_name: str = "output",
        temperature: float | None = None,
        max_tokens: int = 6000,
        tag: str = "",
    ) -> Response:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if response_schema is not None and self.capabilities.json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": response_schema, "strict": True},
            }
        data = self._post("/chat/completions", payload)
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
            finish = choice.get("finish_reason") or "stop"
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"응답 형식이 예상과 다릅니다: {str(data)[:300]}") from e
        u = data.get("usage") or {}
        return Response(
            text=text,
            finish_reason="length" if finish == "length" else ("refusal" if finish == "content_filter" else "stop"),
            usage=Usage(int(u.get("prompt_tokens", 0) or 0), int(u.get("completion_tokens", 0) or 0)),
            model=data.get("model", self.model),
            raw=data,
        )

    def count_tokens(self, messages: list[Message]) -> int | None:
        if self._tokenize_ok is False:
            return None
        base = self.cfg.base_url.rstrip("/")
        root = base[: -len("/v1")] if base.endswith("/v1") else base
        try:
            r = self.client.post(root + "/tokenize", json={"model": self.model, "messages": [{"role": m.role, "content": m.content} for m in messages]})
            if r.status_code >= 400:
                self._tokenize_ok = False
                return None
            data = r.json()
            n = data.get("count") or data.get("num_tokens") or (len(data["tokens"]) if "tokens" in data else None)
            self._tokenize_ok = n is not None
            return int(n) if n is not None else None
        except (httpx.HTTPError, ValueError, KeyError):
            self._tokenize_ok = False
            return None
