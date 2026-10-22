"""구조화 출력 공통 계층 (설계 9.4).

1. provider가 JSON 스키마를 지원하면 스키마를 넘겨 제약 디코딩
2. 아니면 프롬프트에 스키마를 넣고 응답에서 JSON을 뽑는다. 실패하면 오류를 붙여 최대 max_repair회 재요청
3. code_format="fenced"면 메타데이터는 JSON, 코드는 이름 붙은 펜스 블록으로 받는다
응답이 출력 한도에 걸려 잘리면(length) 출력 한도를 두 배로 늘려 1회 다시 요청한다.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from genut.llm.provider import ChatProvider, Message, Response, Usage

T = TypeVar("T", bound=BaseModel)

_DROP = {"default", "title", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
         "minLength", "maxLength", "minItems", "maxItems", "uniqueItems", "pattern", "format", "examples"}


class StructuredError(Exception):
    def __init__(self, message: str, kind: str = "invalid", result: StructuredResult | None = None) -> None:
        super().__init__(message)
        self.kind = kind  # invalid | refusal | length
        self.result = result


def _strictify(node: Any) -> Any:
    if isinstance(node, dict):
        out = {k: _strictify(v) for k, v in node.items() if k not in _DROP}
        if out.get("type") == "object" or "properties" in out:
            props = out.get("properties", {})
            out["type"] = "object"
            out["additionalProperties"] = False
            out["required"] = list(props.keys())
        return out
    if isinstance(node, list):
        return [_strictify(x) for x in node]
    return node


def strict_schema(model: type[BaseModel], exclude: set[str] | None = None) -> dict[str, Any]:
    """두 provider(Claude, OpenAI 호환)가 모두 받는 엄격한 스키마.

    모든 객체에 additionalProperties: false, 모든 속성을 required로 둔다. 선택 필드는 null 허용으로 표현된다.
    exclude는 펜스 모드에서 코드 필드를 스키마에서 빼는 데 쓴다 ("cases.body" 형태도 가능).
    """
    schema = _strictify(copy.deepcopy(model.model_json_schema()))
    for path in exclude or set():
        _remove_field(schema, path.split("."))
    return schema


def _remove_field(schema: dict, parts: list[str]) -> None:
    node = schema
    for i, p in enumerate(parts):
        node = _deref(schema, node)
        props = node.get("properties", {})
        if p not in props:
            return
        if i == len(parts) - 1:
            del props[p]
            node["required"] = [r for r in node.get("required", []) if r != p]
            return
        nxt = _deref(schema, props[p])
        node = nxt.get("items", nxt)


def _deref(root: dict, node: dict) -> dict:
    if "$ref" in node:
        return root.get("$defs", {}).get(node["$ref"].split("/")[-1], node)
    return node


def extract_json(text: str) -> Any:
    """응답 텍스트에서 JSON 값을 찾는다. 통째 JSON → ```json 블록 → 처음 나오는 균형 잡힌 {...} 순."""
    t = text.strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    m = re.search(r"```(?:json)?\s*\n(.*?)```", t, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            pass
    start = t.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start : i + 1])
                    except ValueError:
                        break
        start = t.find("{", start + 1)
    raise ValueError("응답에서 JSON 객체를 찾지 못했습니다.")


@dataclass(frozen=True)
class FencedSpec:
    """펜스 모드에서 코드가 들어갈 필드. items가 있으면 목록의 각 항목, 없으면 최상위 필드 하나."""

    code: str
    items: str | None = None
    key: str | None = None

    def exclude_path(self) -> str:
        return f"{self.items}.{self.code}" if self.items else self.code


_FENCE = re.compile(r"```[^\n`]*\n(.*?)```", re.S)
_CASE_HDR = re.compile(r"^\s*//\s*case:\s*([A-Za-z_]\w*)\s*$", re.M)


def parse_fenced(text: str, spec: FencedSpec) -> dict[str, Any]:
    head = _FENCE.split(text, maxsplit=1)[0] if "```" in text else text
    try:
        data = extract_json(head)
    except ValueError:
        data = extract_json(text)
    blocks = [b for b in _FENCE.findall(text) if not b.lstrip().startswith("{")]
    if spec.items:
        named: dict[str, str] = {}
        for b in blocks:
            m = _CASE_HDR.search(b)
            if m:
                named[m.group(1)] = _CASE_HDR.sub("", b, count=1).strip("\n")
        for item in data.get(spec.items, []) or []:
            if spec.code not in item or not item.get(spec.code):
                item[spec.code] = named.get(str(item.get(spec.key, "")), "")
    elif blocks and not data.get(spec.code):
        data[spec.code] = blocks[0].strip("\n")
    return data


def format_instructions(schema: dict, fenced: FencedSpec | None) -> str:
    s = json.dumps(schema, ensure_ascii=False)
    if fenced is None:
        return (
            "Respond with a single JSON object that matches this JSON Schema. "
            "Output only the JSON, with no prose and no code fences.\n\nJSON Schema:\n" + s
        )
    if fenced.items:
        code = (
            f"Then, for each item in `{fenced.items}`, output its `{fenced.code}` as a separate fenced code block whose "
            f"first line is `// case: <{fenced.key}>`."
        )
    else:
        code = f"Then output `{fenced.code}` as one fenced code block."
    return (
        "First output a JSON object that matches this JSON Schema (the code is NOT inside the JSON). "
        + code
        + "\n\nJSON Schema:\n"
        + s
    )


@dataclass
class StructuredResult:
    value: Any
    text: str
    usage: Usage = field(default_factory=Usage)
    attempts: int = 1
    path: str = "native"  # native | parse | fenced
    transcript: list[tuple[list[Message], Response]] = field(default_factory=list)


def _repair_message(err: Exception) -> str:
    detail = str(err)
    if isinstance(err, ValidationError):
        detail = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in err.errors()[:8])
    return f"Your previous response could not be used: {detail}\nReturn the corrected output only, following the required format exactly."


def ask_structured(
    provider: ChatProvider,
    messages: list[Message],
    out: type[T],
    *,
    tag: str = "",
    temperature: float | None = None,
    max_tokens: int = 6000,
    max_repair: int = 2,
    code_format: str = "json",
    fenced: FencedSpec | None = None,
) -> StructuredResult:
    use_fenced = code_format == "fenced" and fenced is not None
    schema = strict_schema(out, {fenced.exclude_path()} if use_fenced else None)  # type: ignore[union-attr]
    native = provider.capabilities.json_schema and not use_fenced
    path = "fenced" if use_fenced else ("native" if native else "parse")
    msgs = list(messages)
    if not native:
        instr = format_instructions(schema, fenced if use_fenced else None)
        msgs = msgs[:-1] + [Message(msgs[-1].role, msgs[-1].content + "\n\n" + instr)] if msgs and msgs[-1].role == "user" else msgs + [Message("user", instr)]
    result = StructuredResult(value=None, text="", path=path)
    cur_max = max_tokens
    length_retry_done = False
    repairs = 0
    while True:
        resp = provider.chat(
            msgs,
            response_schema=schema if native else None,
            schema_name=out.__name__,
            temperature=temperature if provider.capabilities.supports_temperature else None,
            max_tokens=cur_max,
            tag=tag,
        )
        result.usage = result.usage + resp.usage
        result.transcript.append((list(msgs), resp))
        result.text = resp.text
        if resp.finish_reason == "refusal":
            raise StructuredError("모델이 응답을 거절했습니다.", "refusal", result)
        if resp.finish_reason == "length" and not length_retry_done:
            length_retry_done = True
            cur_max *= 2
            result.attempts += 1
            continue
        try:
            data = parse_fenced(resp.text, fenced) if use_fenced else extract_json(resp.text)  # type: ignore[arg-type]
            result.value = out.model_validate(data)
            return result
        except (ValueError, ValidationError) as e:
            if repairs >= max_repair:
                kind = "length" if resp.finish_reason == "length" else "invalid"
                raise StructuredError(f"구조화 출력 실패({result.attempts}회 시도): {e}", kind, result) from e
            repairs += 1
            result.attempts += 1
            msgs = msgs + [Message("assistant", resp.text), Message("user", _repair_message(e))]
