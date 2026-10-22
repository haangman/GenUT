"""테스트·dry-run용 가짜 provider (설계 9.2, 13.2).

응답을 정하는 방법은 셋이다.
1. responder(tag, messages, schema) 콜백
2. script: {태그: [응답, ...]} — 태그별로 순서대로 소비하고, 마지막 응답은 계속 반복. "*"는 기본값
3. 둘 다 없으면 스키마를 만족하는 최소 응답을 만든다 (dry-run)
응답은 dict(JSON으로 직렬화), str, Response 중 하나다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from genut.llm.provider import Capabilities, Message, Response, Usage
from genut.llm.tokens import estimate_tokens

Responder = Callable[[str, list[Message], dict | None], Any]


def minimal_from_schema(schema: dict, root: dict | None = None) -> Any:
    root = root or schema
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        return minimal_from_schema(root.get("$defs", {}).get(name, {}), root)
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    if "anyOf" in schema:
        opts = [o for o in schema["anyOf"] if o.get("type") != "null"]
        return minimal_from_schema(opts[0], root) if opts else None
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "null")
    if t == "object" or "properties" in schema:
        props = schema.get("properties", {})
        return {k: minimal_from_schema(v, root) for k, v in props.items() if k in schema.get("required", props.keys())}
    return {"string": "", "integer": 0, "number": 0, "boolean": False, "array": [], "null": None}.get(t or "null")


class FakeProvider:
    name = "fake"

    def __init__(
        self,
        responder: Responder | None = None,
        script: dict[str, list[Any]] | None = None,
        json_schema: bool = True,
        max_context: int = 32_768,
        model: str = "fake-model",
    ) -> None:
        self.responder = responder
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.model = model
        self.capabilities = Capabilities(native_tools=False, json_schema=json_schema, max_context=max_context)
        self.calls: list[dict[str, Any]] = []

    @classmethod
    def from_file(cls, path: Path, **kw: Any) -> FakeProvider:
        return cls(script=json.loads(path.read_text(encoding="utf-8")), **kw)

    def _next(self, tag: str) -> Any:
        for key in (tag, "*"):
            q = self.script.get(key)
            if q:
                return q.pop(0) if len(q) > 1 else q[0]
        return None

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
        self.calls.append({"tag": tag, "messages": list(messages), "schema": response_schema, "temperature": temperature})
        out: Any = None
        if self.responder is not None:
            out = self.responder(tag, messages, response_schema)
        if out is None:
            out = self._next(tag)
        if out is None:
            out = minimal_from_schema(response_schema) if response_schema else "{}"
        if isinstance(out, Response):
            return out
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        used_in = sum(estimate_tokens(m.content) for m in messages)
        return Response(text=text, usage=Usage(used_in, estimate_tokens(text)), model=self.model)

    def count_tokens(self, messages: list[Message]) -> int | None:
        return None
