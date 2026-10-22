"""단정 실패 요약의 공통 부분 (gtest_json, ktap).

단정 하나를 한 줄로 요약한다. 같은 종류의 실패는 프레임워크가 달라도 같은 모양이 되게 한다.

    같지 않음: rb_push(&rb, "abcdef", 6) [-28] ≠ RB_OK [0]     [ ] 안은 실행 중 계산된 값
    비교 실패: push(1) < -1 아님 (값 0, -1)
    참·거짓 불일치: push(6) == 0 → false (기대 true)

기대값·실제값은 어느 쪽인지 확실할 때만 채운다. 같음 비교는 인자 순서에 약속이 없으므로 한쪽에만 함수 호출이
있을 때 그쪽을 실제값으로 본다. 잘못 채우면 실제값 베끼기 검사(review.copied_actual)가 진짜 베끼기를 놓친다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_EXPR = 100
MAX_SUMMARY = 300
_CALL = re.compile(r"[A-Za-z_]\w*\s*\(")


@dataclass
class Assertion:
    """실패한 단정 하나."""

    summary: str
    expected: str | None = None
    actual: str | None = None
    observed: list[str] = field(default_factory=list)


def short(text: str, n: int = MAX_EXPR) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def with_message(a: Assertion, extra: list[str]) -> Assertion:
    """단정에 붙인 사용자 메시지(<< "..." 또는 KUNIT_*_MSG)를 ' — 메시지'로 덧붙인다."""
    if extra:
        a.summary += f" — {short(extra[0], 80)}"
    return a


def equality(sides: list[tuple[str, str]]) -> Assertion:
    """같음 비교. sides: [(식, 값)]. 값이 없으면(리터럴) 식만 쓴다."""
    shown = " ≠ ".join(f"{short(e)} [{short(v, 40)}]" if v else short(e) for e, v in sides)
    out = Assertion(f"같지 않음: {shown}", observed=[v for _, v in sides if v])
    calls = [i for i, (e, _) in enumerate(sides) if _CALL.search(e)]
    if len(sides) == 2 and len(calls) == 1:
        a, x = sides[calls[0]], sides[1 - calls[0]]
        out.actual = a[1] or a[0]
        out.expected = x[1] or x[0]
    return out


def comparison(left: str, op: str, right: str, left_value: str, right_value: str) -> Assertion:
    """크기·다름 비교. 어느 쪽이 기대값인지 알 수 없으므로 기대값·실제값은 비운다."""
    return Assertion(f"비교 실패: {short(left)} {op} {short(right)} 아님 (값 {left_value}, {right_value})", observed=[left_value, right_value])


def truth(expr: str, actual: str, expected: str) -> Assertion:
    return Assertion(f"참·거짓 불일치: {short(expr)} → {actual} (기대 {expected})", expected=expected, actual=actual, observed=[actual])


def combine(descs: list[Assertion]) -> Assertion:
    """케이스 하나의 실패들. 요약·기대값·실제값은 첫 실패에서, 관찰값은 모든 실패에서 모은다."""
    if not descs:
        return Assertion("")
    head = descs[0]
    more = f" 외 {len(descs) - 1}건" if len(descs) > 1 else ""
    summary = head.summary if len(head.summary) + len(more) <= MAX_SUMMARY else head.summary[: MAX_SUMMARY - len(more) - 1] + "…"
    observed: list[str] = []
    for d in descs:
        observed += [v for v in d.observed if v not in observed]
    return Assertion(summary + more, head.expected, head.actual, observed)
