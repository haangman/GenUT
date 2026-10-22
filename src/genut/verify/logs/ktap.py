"""KTAP 파서 (설계 8.2). 우리 스위트 블록만 뽑고 케이스별 결과와 실패 상세를 모은다.

    KTAP version 1
    # Subtest: ring_buffer_rb_push_test
    1..3
    ok 1 test_a
    # test_b: EXPECTATION FAILED at drivers/foo/x.c:34
    Expected rb_push(&rb, data, 8) == -28, but
        rb_push(&rb, data, 8) == 0 (0x0)
    not ok 2 test_b
    # test_c: try timed out
    not ok 3 test_c

단정 실패는 커널 lib/kunit/assert.c가 찍는 형식으로 종류를 가려 한 줄로 요약한다 (gtest와 같은 모양, assertion.py).

    Expected L == R, but / L == 값 (0x..) / R == 값        같지 않음: L [값] ≠ R [값]   (리터럴 쪽은 값 줄이 없음)
    Expected L < R, but ...                                 비교 실패: L < R 아님 (값 a, b)
    Expected C to be true, but is false                     참·거짓 불일치: C → false (기대 true)
    Expected P is not null, but is                          NULL 포인터: P → NULL
    Expected P is not error, but is: -12                    오류 포인터: P → -12
    KUNIT_FAIL(test, "메시지")                               KUNIT_FAIL: 메시지
_MSG 매크로의 메시지는 값 줄 뒤에 오며 ' — 메시지'로 덧붙는다. MEMEQ의 16진 덤프 줄은 요약에서 뺀다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from genut.models import CaseResult
from genut.verify.logs.assertion import MAX_SUMMARY, Assertion, combine, comparison, equality, short, truth, with_message

_TS = re.compile(r"^\s*\[\s*\d+\.\d+\]\s?")
_RESULT = re.compile(r"^(?P<indent>\s*)(?P<ok>ok|not ok)\s+(?P<num>\d+)\s*-?\s*(?P<name>[^#\s]+)?(?:\s*#\s*(?P<directive>.*))?$")
_SUBTEST = re.compile(r"^(?P<indent>\s*)#\s*Subtest:\s*(?P<name>\S+)")
_FAIL = re.compile(r"^\s*#\s*(?P<case>\w+):\s*(?P<kind>EXPECTATION|ASSERTION) FAILED at (?P<file>[^:\s]+):(?P<line>\d+)")
_TIMEOUT = re.compile(r"^\s*#\s*(?P<case>\w+):\s*(?:try timed out|.*timed out)")
_DIAG = re.compile(r"^\s*#\s*(?P<case>\w+):\s*(?P<msg>.*)$")
_VALUE = re.compile(r"==\s*(?P<v>.+?)(?:\s*\((?:0x)?[0-9a-fA-F]+\))?\s*$")
_K_BIN = re.compile(r"^Expected (?P<l>.+?) (?P<op>==|!=|<=|>=|<|>) (?P<r>.+), but$")
_K_UNARY = re.compile(r"^Expected (?P<c>.+) to be (?P<exp>true|false), but is (?P<act>true|false)$")
_K_NULL = re.compile(r"^Expected (?P<p>.+) is not null, but is$")
_K_ERR = re.compile(r"^Expected (?P<p>.+) is not error, but is: (?P<e>-?\d+)$")
_K_HEX = re.compile(r"^(?:[<>]?[0-9a-fA-F]{2}[<>]?\s*)+$")  # MEMEQ의 16진 덤프 줄
_HEX_SUFFIX = re.compile(r"\s*\(0x[0-9a-fA-F]+\)$")


def strip_timestamps(text: str) -> list[str]:
    return [_TS.sub("", line.rstrip("\r")) for line in text.splitlines()]


@dataclass
class KtapSuite:
    name: str
    results: dict[str, CaseResult] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    ok: bool | None = None
    lines: list[str] = field(default_factory=list)  # 이 스위트 블록의 원본 줄 (축약용)


def _observed(block: list[str]) -> list[str]:
    """실패 블록의 값 줄(`    foo(...) == 0 (0x0)`)에서 값을 모은다. 머리 줄 `Expected a == b, but`은 식이라 뺀다."""
    vals = []
    for line in block[1:]:
        if line.strip().startswith("Expected "):
            continue
        m = _VALUE.search(line)
        if m:
            vals.append(m.group("v").strip())
    return vals


def _binary(m: re.Match, rest: list[str]) -> Assertion:
    left, op, right = m.group("l"), m.group("op"), m.group("r")
    values: dict[str, str] = {}
    extra: list[str] = []
    for line in rest:
        for side in (left, right):
            if side not in values and line.startswith(side + " =="):
                values[side] = _HEX_SUFFIX.sub("", line[len(side) + 3:].strip())  # MEMEQ면 값 없이 "L ==" 뒤에 덤프
                break
        else:
            if not _K_HEX.match(line):
                extra.append(line)
    lv, rv = values.get(left, ""), values.get(right, "")
    if op == "==":
        return with_message(equality([(left, lv), (right, rv)]), extra)
    return with_message(comparison(left, op, right, lv or left, rv or right), extra)


def describe_kunit(block: list[str]) -> Assertion:
    """실패 덩어리 하나("# case: EXPECTATION FAILED at ..." 줄 + 본문)를 요약한다. 모르는 형식이면 본문 첫 줄을 쓴다."""
    body = [x.strip() for x in block[1:] if x.strip()]
    if not body:
        return Assertion(short(block[0].strip().lstrip("# ") if block else "", MAX_SUMMARY))
    first, rest = body[0], body[1:]
    m = _K_BIN.match(first)
    if m:
        return _binary(m, rest)
    m = _K_UNARY.match(first)
    if m:
        return with_message(truth(m.group("c"), m.group("act"), m.group("exp")), rest)
    m = _K_NULL.match(first)
    if m:
        return with_message(Assertion(f"NULL 포인터: {short(m.group('p'))} → NULL (기대: 유효한 포인터)", actual="NULL", observed=["NULL"]), rest)
    m = _K_ERR.match(first)
    if m:
        e = m.group("e")
        return with_message(Assertion(f"오류 포인터: {short(m.group('p'))} → {e} (기대: 유효한 포인터)", actual=e, observed=[e]), rest)
    if not first.startswith("Expected "):  # KUNIT_FAIL은 메시지만 찍는다
        return Assertion(f"KUNIT_FAIL: {short(' '.join(body), 200)}")
    return Assertion(short(first, MAX_SUMMARY), observed=_observed(block))


def parse_ktap(text: str, suites: set[str] | None = None) -> dict[str, KtapSuite]:
    """suites가 있으면 그 이름의 스위트만 돌려준다."""
    lines = strip_timestamps(text)
    out: dict[str, KtapSuite] = {}
    current: KtapSuite | None = None
    cur_indent = ""
    pending: dict[str, list[list[str]]] = {}
    block: list[str] | None = None
    block_case = ""
    timed_out: set[str] = set()

    def close_block() -> None:
        nonlocal block
        if block is not None and block_case:
            pending.setdefault(block_case, []).append(block)
        block = None

    for line in lines:
        m = _SUBTEST.match(line)
        if m:
            close_block()
            name = m.group("name")
            if suites is None or name in suites:
                current = KtapSuite(name=name)
                out[name] = current
                cur_indent = m.group("indent")
                pending, timed_out = {}, set()
            elif current is not None and len(m.group("indent")) > len(cur_indent):
                pass  # 파라미터화 케이스의 하위 서브테스트
            else:
                current = None
            continue
        if current is None:
            continue
        current.lines.append(line)
        m = _FAIL.match(line)
        if m:
            close_block()
            block = [line]
            block_case = m.group("case")
            continue
        m = _TIMEOUT.match(line)
        if m:
            close_block()
            timed_out.add(m.group("case"))
            continue
        m = _RESULT.match(line)
        if m:
            close_block()
            indent = m.group("indent")
            name = m.group("name") or ""
            if len(indent) < len(cur_indent) or (len(indent) == len(cur_indent) and name == current.name):
                current.ok = m.group("ok") == "ok"
                current = None
                continue
            if len(indent) > len(cur_indent) + 4:
                continue  # 하위 서브테스트 결과
            directive = (m.group("directive") or "").strip()
            if name in timed_out:
                status = "timeout"
            elif m.group("ok") == "ok":
                status = "skip" if directive.upper().startswith("SKIP") else "pass"
            else:
                status = "fail"
            blocks = pending.pop(name, [])
            details = "\n".join("\n".join(b) for b in blocks)
            first = blocks[0][0] if blocks else ""
            fm = _FAIL.match(first) if first else None
            a = combine([describe_kunit(b) for b in blocks]) if blocks else Assertion("try timed out" if status == "timeout" else "")
            current.results[name] = CaseResult(
                name=name,
                status=status,
                message=a.summary,
                expected=a.expected,
                actual=a.actual,
                details=details,
                observed=a.observed,
                file=fm.group("file") if fm else "",
                line=int(fm.group("line")) if fm else 0,
            )
            current.order.append(name)
            continue
        if block is not None:
            if _DIAG.match(line) and not line.strip().startswith(f"# {block_case}:"):
                close_block()
            else:
                block.append(line)
    close_block()
    return out


def suite_summary(s: KtapSuite) -> str:
    rows = [f"{'ok' if r.status == 'pass' else 'not ok'} {r.name}" + (f"  # {r.status}" if r.status not in ("pass", "fail") else "") for r in s.results.values()]
    return f"# Subtest: {s.name}\n" + "\n".join(rows)
