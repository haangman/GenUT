"""코드가 하는 리뷰 검사 (설계 7.9). 케이스가 통과하는 즉시 돌린다.

LLM보다 확실하고 비용이 없다. 걸리면 그 케이스는 pass가 아니라 fail(review_reject)로 Fixer에 돌아간다.
- no_assertion: EXPECT_*/ASSERT_*/KUNIT_EXPECT_*/KUNIT_ASSERT_* 가 하나도 없음
- always_true: EXPECT_TRUE(true), 같은 식끼리 비교(EXPECT_EQ(x, x)), 상수끼리 비교
- rejected_pattern: 규칙 md "리뷰에서 거부할 패턴"의 정규식
- target_not_called: 본문(과 본문이 쓰는 헬퍼)에 대상 함수 호출이 없음
- copied_actual: 직전 실패의 실제값을 그대로 기대값으로 옮김. 소스 반환 지점 근거가 없으면 소스 버그 의심으로 돌린다
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from genut.rules import Rules
from genut.state import TestCase, used_helpers

_ASSERT = re.compile(r"\b(?P<name>(?:KUNIT_)?(?:EXPECT|ASSERT)_[A-Z_]+)\s*\(")
_LITERAL = re.compile(r"""^(?:[-+]?(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d*)?)[uUlLfF]*|'(?:\\.|[^'])'|"(?:\\.|[^"])*"|true|false|NULL|nullptr)$""")
_SAME_IS_TRUE = {"EQ", "LE", "GE", "STREQ", "STRCASEEQ", "FLOAT_EQ", "DOUBLE_EQ", "PTR_EQ"}
_COMPARE = {"EQ", "NE", "LT", "LE", "GT", "GE", "STREQ", "STRNE", "STRCASEEQ", "STRCASENE", "FLOAT_EQ", "DOUBLE_EQ", "PTR_EQ", "PTR_NE"}


@dataclass(frozen=True)
class Finding:
    check: str
    detail: str


def _args(text: str, start: int) -> list[str] | None:
    """start(여는 괄호 다음)부터 짝이 맞는 닫는 괄호까지의 최상위 인자."""
    depth, cur, out, i = 0, [], [], start
    quote = ""
    while i < len(text):
        ch = text[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            if depth == 0:
                out.append("".join(cur).strip())
                return out
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    return None


def assertions(body: str) -> list[tuple[str, list[str]]]:
    """(매크로 이름, 인자) 목록. KUnit은 첫 인자 test를 뺀다."""
    out = []
    for m in _ASSERT.finditer(body):
        args = _args(body, m.end())
        if args is None:
            continue
        name = m.group("name")
        if name.startswith("KUNIT_") and args and args[0].strip() == "test":
            args = args[1:]
        out.append((name, args))
    return out


def _norm(expr: str) -> str:
    return re.sub(r"\s+", "", expr)


def _always_true(name: str, args: list[str]) -> bool:
    kind = name.split("_", 2)[-1] if name.startswith("KUNIT_") else name.split("_", 1)[1]
    if kind == "TRUE" and args:
        return _norm(args[0]) in ("true", "1", "!0", "!false")
    if kind == "FALSE" and args:
        return _norm(args[0]) in ("false", "0", "!1", "!true")
    if kind in _COMPARE and len(args) >= 2:
        a, b = _norm(args[0]), _norm(args[1])
        if a == b and kind in _SAME_IS_TRUE:
            return True
        return bool(_LITERAL.match(a) and _LITERAL.match(b))
    return False


def _calls(text: str, function: str, member: bool = False, constructor: bool = False) -> bool:
    """대상 호출이 있는가. 자유 함수는 다른 객체의 같은 이름 멤버(x.f()) 호출을 세지 않고, 멤버 함수는 obj.f(), p->f(), C::f()를 센다.
    생성자는 클래스 이름이 나오면 (C c(1), C(1), new C) 부른 것으로 본다."""
    if constructor:
        return re.search(rf"(?<!\w){re.escape(function)}\b", text) is not None
    before = r"(?<!\w)" if member else r"(?<![\w.>])"
    return re.search(rf"{before}{re.escape(function)}\s*\(", text) is not None


def copied_actual(case: TestCase) -> list[str]:
    """직전 실패의 실제값과 똑같은 값이 이번 수정에서 새로 기대값 자리에 들어갔으면 그 값들."""
    if not case.previous_body or case.last_failure is None or not case.last_failure.observed:
        return []
    before = {_norm(a) for _, args in assertions(case.previous_body) for a in args}
    observed = {_norm(v) for v in case.last_failure.observed if v.strip()}
    if case.last_failure.expected:
        observed.discard(_norm(case.last_failure.expected))
    out = []
    for _, args in assertions(case.body):
        for a in args:
            n = _norm(a)
            if n in observed and n not in before and n not in out:
                out.append(n)
    return out


def justified(evidence: str, return_lines: list[int]) -> bool:
    """Fixer의 근거가 소스 반환 지점(줄 번호)을 가리키는가."""
    return any(re.search(rf"(?<!\d){n}(?!\d)", evidence) for n in return_lines)


def review_case(case: TestCase, function: str, helpers: dict[str, str], rules: Rules, member: bool = False, constructor: bool = False) -> list[Finding]:
    found: list[Finding] = []
    asserts = assertions(case.body)
    if not asserts:
        found.append(Finding("no_assertion", "단정(EXPECT_*/ASSERT_*/KUNIT_EXPECT_*)이 하나도 없습니다."))
    for name, args in asserts:
        if _always_true(name, args):
            found.append(Finding("always_true", f"결과가 항상 같은 단정입니다: {name}({', '.join(args)})"))
            break
    for rx in rules.compiled_rejects():
        m = rx.search(case.body)
        if m:
            found.append(Finding("rejected_pattern", f"규칙 md가 거부하는 패턴입니다: `{rx.pattern}` (본문: {m.group(0)})"))
    code = case.body + "\n" + "\n".join(helpers[h] for h in used_helpers([case.body], helpers))
    if not _calls(code, function, member, constructor):
        found.append(Finding("target_not_called", f"대상 함수 {function}을(를) 호출하지 않습니다."))
    copied = copied_actual(case)
    if copied:
        found.append(Finding("copied_actual", "직전 실패의 실제값을 기대값으로 옮겼습니다: " + ", ".join(copied)))
    return found
