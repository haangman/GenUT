import json
from types import SimpleNamespace
from typing import Literal

import httpx
import pytest
from pydantic import BaseModel

from genut.config import AnthropicConfig, OpenAICompatConfig
from genut.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider
from genut.llm.fake_provider import FakeProvider, minimal_from_schema
from genut.llm.openai_compat_provider import OpenAICompatProvider
from genut.llm.prompt_builder import OPTIONAL, REQUIRED, SHRINKABLE, ContextOverflow, Fragment, PromptBuilder
from genut.llm.provider import Message, Response, TransportError
from genut.llm.structured import FencedSpec, StructuredError, ask_structured, extract_json, parse_fenced, strict_schema
from genut.llm.tokens import TokenCounter, estimate_tokens, load_calibration, save_calibration


class Case(BaseModel):
    name: str
    purpose: str = ""
    body: str


class Out(BaseModel):
    cases: list[Case]
    note: str | None = None


class Fix(BaseModel):
    strategy: Literal["fix_case", "drop_case"]
    new_body: str = ""


MSGS = [Message("system", "sys"), Message("user", "write tests")]
GOOD = {"cases": [{"name": "test_a", "purpose": "p", "body": "EXPECT_EQ(f(), 1);"}], "note": None}


def test_strict_schema_shape():
    s = strict_schema(Out)
    assert s["additionalProperties"] is False and s["required"] == ["cases", "note"]
    case = s["$defs"]["Case"]
    assert case["additionalProperties"] is False and set(case["required"]) == {"name", "purpose", "body"}
    assert "default" not in json.dumps(s) and "title" not in case
    s2 = strict_schema(Out, {"cases.body"})
    assert "body" not in s2["$defs"]["Case"]["properties"]


def test_minimal_from_schema_validates():
    Out.model_validate(minimal_from_schema(strict_schema(Out)))
    Fix.model_validate(minimal_from_schema(strict_schema(Fix)))


def test_native_path_passes_schema():
    fp = FakeProvider(script={"tw": [GOOD]})
    r = ask_structured(fp, MSGS, Out, tag="tw")
    assert r.path == "native" and r.value.cases[0].name == "test_a"
    assert fp.calls[0]["schema"]["type"] == "object"


def test_parse_path_repairs_invalid_output():
    fp = FakeProvider(script={"tw": ["sorry, here: {\"cases\": [{\"name\": 1}]}", "```json\n" + json.dumps(GOOD) + "\n```"]}, json_schema=False)
    r = ask_structured(fp, MSGS, Out, tag="tw")
    assert r.path == "parse" and r.attempts == 2
    assert fp.calls[0]["schema"] is None
    assert "JSON Schema" in fp.calls[0]["messages"][-1].content
    assert "could not be used" in fp.calls[1]["messages"][-1].content


def test_repair_exhausted_raises():
    fp = FakeProvider(script={"*": ["not json"]}, json_schema=False)
    with pytest.raises(StructuredError) as ei:
        ask_structured(fp, MSGS, Out, max_repair=2)
    assert ei.value.result.attempts == 3


def test_length_retry_doubles_max_tokens():
    seen = []

    def responder(tag, msgs, schema):
        seen.append(len(seen))
        return Response(text='{"cases": [', finish_reason="length") if len(seen) == 1 else json.dumps(GOOD)

    fp = FakeProvider(responder=responder)
    r = ask_structured(fp, MSGS, Out, max_tokens=100)
    assert r.value.cases and len(seen) == 2


def test_refusal_raises():
    fp = FakeProvider(responder=lambda *a: Response(text="", finish_reason="refusal"))
    with pytest.raises(StructuredError) as ei:
        ask_structured(fp, MSGS, Out)
    assert ei.value.kind == "refusal"


def test_fenced_mode_merges_code_blocks():
    text = (
        '{"cases": [{"name": "test_a", "purpose": "p"}, {"name": "test_b", "purpose": "q"}], "note": null}\n'
        "```c\n// case: test_b\nKUNIT_EXPECT_EQ(test, f(), 2);\n```\n"
        "```c\n// case: test_a\nKUNIT_EXPECT_EQ(test, f(), 1);\n```\n"
    )
    fp = FakeProvider(script={"*": [text]})
    r = ask_structured(fp, MSGS, Out, code_format="fenced", fenced=FencedSpec(code="body", items="cases", key="name"))
    assert r.path == "fenced"
    bodies = {c.name: c.body for c in r.value.cases}
    assert bodies == {"test_a": "KUNIT_EXPECT_EQ(test, f(), 1);", "test_b": "KUNIT_EXPECT_EQ(test, f(), 2);"}
    assert fp.calls[0]["schema"] is None


def test_fenced_single_field():
    d = parse_fenced('{"strategy": "fix_case"}\n```c\nx = 1;\n```', FencedSpec(code="new_body"))
    assert Fix.model_validate(d).new_body == "x = 1;"


@pytest.mark.parametrize(
    "text",
    ['{"a": 1}', 'here you go:\n```json\n{"a": 1}\n```', 'blah {"a": 1} trailing', 'x {"s": "}"} {"a": 1}'],
)
def test_extract_json_variants(text):
    assert extract_json(text) in ({"a": 1}, {"s": "}"})


def test_token_estimate_is_conservative():
    eng = "The quick brown fox jumps over the lazy dog. " * 10
    assert estimate_tokens(eng) >= len(eng) / 4
    assert estimate_tokens("한국어 문장") >= 7
    assert TokenCounter(calibration=0.5)("abc") == estimate_tokens("abc")  # 1보다 작은 보정은 무시


def test_calibration_roundtrip(tmp_path):
    assert load_calibration(tmp_path, "m") == 1.0
    f = save_calibration(tmp_path, "m", estimated=100, actual=130)
    assert f > 1.3 and load_calibration(tmp_path, "m") == f
    assert save_calibration(tmp_path, "m2", estimated=100, actual=80) == 1.0


def _builder(limit_tokens):
    return PromptBuilder(TokenCounter(), max_context=limit_tokens + 100, output_reserve=100, safety_margin=0.0)


def test_builder_fits_without_trims():
    b = _builder(1000).build([Fragment("role", "x" * 30, REQUIRED, "system"), Fragment("in", "y" * 30)])
    assert b.trims == [] and b.system == "x" * 30


def test_builder_drops_optional_then_shrinks():
    frags = [
        Fragment("role", "r" * 300, REQUIRED, "system"),
        Fragment("analysis", "a" * 900, SHRINKABLE, shrinkers=[lambda: "a" * 300, lambda: ""]),
        Fragment("passed", "p" * 600, SHRINKABLE, shrinkers=[lambda: "p" * 60]),
        Fragment("examples", "e" * 900, OPTIONAL),
    ]
    b = _builder(600).build(frags)  # 토큰 = 글자/3
    names = [(t.name, t.action) for t in b.trims]
    assert names[0] == ("examples", "dropped")
    assert ("passed", "shrunk") in names
    assert b.tokens <= b.limit
    assert "r" * 300 in b.system


def test_builder_overflow_on_required():
    with pytest.raises(ContextOverflow) as ei:
        _builder(100).build([Fragment("rules", "x" * 3000, REQUIRED, "system")])
    assert "rules" in str(ei.value)


# ------------------------------------------------------------- openai compat
def _oc(handler, **kw):
    sleeps = []
    p = OpenAICompatProvider(
        OpenAICompatConfig(base_url="http://gemma.test/v1", api_key_env="", **kw),
        max_retries=3,
        client=httpx.Client(base_url="http://gemma.test/v1", transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )
    return p, sleeps


def test_openai_compat_request_and_retry():
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        if len(calls) < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"a":1}'}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}})

    p, sleeps = _oc(handler)
    r = p.chat(MSGS, response_schema={"type": "object"}, temperature=0.3)
    assert r.text == '{"a":1}' and r.usage.total == 7
    assert sleeps == [1.0, 2.0]
    body = calls[-1]
    assert body["response_format"]["type"] == "json_schema" and body["temperature"] == 0.3
    assert body["messages"][0]["role"] == "system"


def test_openai_compat_gives_up_as_transport_error():
    p, sleeps = _oc(lambda req: httpx.Response(429, text="slow down"))
    with pytest.raises(TransportError):
        p.chat(MSGS)
    assert len(sleeps) == 3


def test_openai_compat_no_schema_when_unsupported_and_length():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]})

    p, _ = _oc(handler, supports_json_schema=False)
    r = p.chat(MSGS, response_schema={"type": "object"})
    assert "response_format" not in seen[0] and r.finish_reason == "length"


def test_openai_compat_tokenize_optional():
    def handler(req):
        if req.url.path == "/tokenize":
            return httpx.Response(200, json={"count": 42})
        return httpx.Response(404)

    p, _ = _oc(handler)
    assert p.count_tokens(MSGS) == 42
    p2, _ = _oc(lambda req: httpx.Response(404))
    assert p2.count_tokens(MSGS) is None


# ------------------------------------------------------------------ anthropic
class _FakeMessages:
    def __init__(self, store):
        self.store = store

    def create(self, **kw):
        self.store.append(kw)
        return SimpleNamespace(
            stop_reason="end_turn",
            model=kw["model"],
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text='{"a": 1}')],
            usage=SimpleNamespace(input_tokens=10, output_tokens=3),
        )

    def count_tokens(self, **kw):
        return SimpleNamespace(input_tokens=17)


def _fake_client(store):
    msgs = _FakeMessages(store)
    return SimpleNamespace(messages=msgs, beta=SimpleNamespace(messages=msgs))


def test_anthropic_provider_request_shape():
    store = []
    p = AnthropicProvider(AnthropicConfig(), "claude-opus-5-5", client=_fake_client(store))
    r = p.chat(MSGS, response_schema={"type": "object"}, temperature=0.9, max_tokens=500)
    kw = store[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["system"] == "sys" and kw["messages"] == [{"role": "user", "content": "write tests"}]
    assert "temperature" not in kw  # Messages API는 샘플링 인자를 받지 않는다
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert kw["output_config"]["effort"] == "medium"
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert kw["max_tokens"] >= 16000
    assert r.text == '{"a": 1}' and r.usage.total == 13
    assert p.count_tokens(MSGS) == 17
    assert p.capabilities.supports_temperature is False


def test_anthropic_without_fallbacks_uses_plain_create():
    store = []
    p = AnthropicProvider(AnthropicConfig(fallbacks=""), "claude-opus-5-5", client=_fake_client(store))
    p.chat(MSGS)
    assert "betas" not in store[0] and "fallbacks" not in store[0]
