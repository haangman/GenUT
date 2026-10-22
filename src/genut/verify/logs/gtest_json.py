"""gtest 결과 파서 (설계 7.5). --gtest_output=json 이 기본이고, 크래시로 JSON이 없으면 stdout으로 대신한다.

단정 실패는 종류별로 식과 값을 뽑아 한 줄 요약을 만든다. 요약은 Fixer 프롬프트·리포트·수정 사례의 메시지 패턴에 쓰인다.

    같지 않음: rb_push(&rb, "abcdef", 6) [-28] ≠ RB_OK [0]        EXPECT_EQ / STREQ / FLOAT_EQ
    비교 실패: push(1) < -1 아님 (값 0, -1)                        EXPECT_NE / LT / LE / GT / GE
    참·거짓 불일치: push(6) == 0 → false (기대 true)                EXPECT_TRUE / FALSE
    조건 불일치: push(6) → -28 (기대: is equal to 0)                EXPECT_THAT
    오차 초과: |1.5 - 1.0| = 0.5 > 1e-3 (값 1.5, 1)                 EXPECT_NEAR
    FAIL(): 메시지 · 예외: C++ exception ...

기대값·실제값은 어느 쪽인지 확실할 때만 채운다. EXPECT_EQ는 인자 순서에 약속이 없으므로 한쪽에만 호출이 있을 때
그쪽을 실제값으로 본다. 잘못 채우면 실제값 베끼기 검사(review.copied_actual)가 진짜 베끼기를 놓친다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from genut.models import CaseResult

_RUN = re.compile(r"^\[ RUN      \] (?P<suite>[\w/]+)\.(?P<case>[\w/]+)")
_OK = re.compile(r"^\[       OK \] (?P<suite>[\w/]+)\.(?P<case>[\w/]+)")
_FAILED = re.compile(r"^\[  FAILED  \] (?P<suite>[\w/]+)\.(?P<case>[\w/]+)(?: \(\d+ ms\))?$")
_SKIPPED = re.compile(r"^\[  SKIPPED \] (?P<suite>[\w/]+)\.(?P<case>[\w/]+)")
# 실패 하나의 머리 줄: stdout은 "file:12: Failure", JSON은 "file:12", 위치를 모르면 "unknown file: Failure"
_LOC = re.compile(r"^(?P<file>(?:[A-Za-z]:)?[^:\n]+):(?P<line>\d+)(?:: Failure)?[ \t]*$", re.M)
_HEAD = re.compile(r"^(?:(?:[A-Za-z]:)?[^:\n]+:\d+(?:: Failure)?|unknown file(?:: Failure)?)[ \t]*$")
_CMP = re.compile(r"^Expected: \((?P<a>.*)\) (?P<op>!=|<=|>=|<|>|==) \((?P<b>.*)\), actual: (?P<va>.*) vs (?P<vb>.*)$")
_NEAR = re.compile(r"^The difference between (?P<a>.+) and (?P<b>.+) is (?P<d>\S+), which exceeds (?P<t>.+), where$")
_EVAL = re.compile(r"^(?P<e>.+) evaluates to (?P<v>.+?)(?:,| and|,? and|\.)?$")
_OF_TYPE = re.compile(r"\s*\(of type [^)]*\)$")
_CALL = re.compile(r"[A-Za-z_]\w*\s*\(")
_ANY_VALUE = re.compile(r"^\s*(?:Which is|Actual):\s*(.+)$")
MAX_EXPR = 100
MAX_SUMMARY = 300


@dataclass
class Assertion:
    """실패한 단정 하나."""

    summary: str
    expected: str | None = None
    actual: str | None = None
    observed: list[str] = field(default_factory=list)


def _utf8(text: str) -> str:
    """gtest JSON은 UTF-8 바이트를 하나씩 \\u00XX로 이스케이프한다. 그렇게 깨진 문자열이면 바이트로 되돌려 다시 읽는다."""
    if not any(0x80 <= ord(ch) <= 0xFF for ch in text) or any(ord(ch) > 0xFF for ch in text):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except UnicodeDecodeError:
        return text


def _short(text: str, n: int = MAX_EXPR) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def split_failures(msg: str) -> list[str]:
    """여러 실패가 이어진 메시지(stdout)를 실패마다 나눈다. 머리 줄로 시작하는 덩어리 단위."""
    blocks: list[list[str]] = []
    for line in msg.splitlines():
        if _HEAD.match(line) or not blocks:
            blocks.append([line])
        else:
            blocks[-1].append(line)
    return ["\n".join(b).strip("\n") for b in blocks if "\n".join(b).strip()]


def _equality(body: list[str]) -> Assertion:
    sides: list[list[str]] = []  # [식, 값]
    extra: list[str] = []
    for line in body[1:]:
        if line.strip() == "With diff:":  # 여러 줄 문자열의 diff는 상세에만 둔다
            break
        if line.startswith("    Which is: ") and sides:
            sides[-1][1] = line[len("    Which is: "):].strip()
        elif line.startswith("  ") and not line.startswith("    ") and len(sides) < 2 and not extra:
            sides.append([line.strip(), ""])
        elif line.strip():
            extra.append(line.strip())
    shown = " ≠ ".join(f"{_short(e)} [{_short(v, 40)}]" if v else _short(e) for e, v in sides)
    summary = f"같지 않음: {shown}" + (f" — {_short(extra[0], 80)}" if extra else "")
    out = Assertion(summary, observed=[v for _, v in sides if v])
    calls = [i for i, (e, _) in enumerate(sides) if _CALL.search(e)]
    if len(sides) == 2 and len(calls) == 1:
        a, x = sides[calls[0]], sides[1 - calls[0]]
        out.actual = a[1] or a[0]
        out.expected = x[1] or x[0]
    return out


def _value_of(body: list[str]) -> Assertion | None:
    fields: dict[str, str] = {}
    for line in body:
        for key in ("Value of", "Expected", "Actual"):
            if line.strip().startswith(key + ":") and key not in fields:
                fields[key] = line.strip()[len(key) + 1:].strip()
    if "Value of" not in fields or "Actual" not in fields:
        return None
    expr, exp, act = fields["Value of"], fields.get("Expected", ""), _OF_TYPE.sub("", fields["Actual"])
    if exp in ("true", "false"):
        return Assertion(f"참·거짓 불일치: {_short(expr)} → {act} (기대 {exp})", expected=exp, actual=act, observed=[act])
    return Assertion(f"조건 불일치: {_short(expr)} → {_short(act, 40)} (기대: {_short(exp, 60)})", expected=exp or None, actual=act, observed=[act])


def _near(body: list[str]) -> Assertion | None:
    m = _NEAR.match(body[0])
    if not m:
        return None
    vals = []
    for line in body[1:]:
        e = _EVAL.match(line.strip())
        if e and e.group("e").strip() in (m.group("a"), m.group("b")):
            vals.append(e.group("v").strip())
    return Assertion(f"오차 초과: |{_short(m.group('a'), 50)} - {_short(m.group('b'), 50)}| = {m.group('d')} > {m.group('t')}"
                     + (f" (값 {', '.join(vals)})" if vals else ""), observed=vals)


def describe_failure(block: str) -> Assertion:
    """실패 덩어리 하나(머리 줄 + 본문)를 요약한다. 모르는 형식이면 본문 첫 줄을 쓴다."""
    lines = block.splitlines()
    body = lines[1:] if lines and _HEAD.match(lines[0]) else lines
    while body and not body[-1].strip():
        body.pop()
    if not body:
        return Assertion(_short(lines[0] if lines else "", MAX_SUMMARY))
    first = body[0].strip()
    if first == "Expected equality of these values:":
        return _equality(body)
    m = _CMP.match(first)
    if m:
        return Assertion(f"비교 실패: {_short(m.group('a'))} {m.group('op')} {_short(m.group('b'))} 아님 (값 {m.group('va')}, {m.group('vb')})",
                         observed=[m.group("va").strip(), m.group("vb").strip()])
    for parse in (_value_of, _near):
        a = parse(body)
        if a is not None:
            return a
    if first == "Failed":
        msg = " ".join(x.strip() for x in body[1:] if x.strip())
        return Assertion(f"FAIL(): {_short(msg, 200)}" if msg else "FAIL()")
    if "exception" in first.lower():
        return Assertion(f"예외: {_short(first, 200)}")
    # 모르는 형식(다른 gtest 버전 등): 첫 줄을 요약으로, 값처럼 보이는 줄은 관찰값으로
    vals = [m.group(1).strip() for m in (_ANY_VALUE.match(x) for x in body) if m]
    return Assertion(_short(first, MAX_SUMMARY), observed=[_OF_TYPE.sub("", v) for v in vals])


def summarize(failures: list[str]) -> Assertion:
    """케이스 하나의 실패들. 요약·기대값·실제값은 첫 실패에서, 관찰값은 모든 실패에서 모은다."""
    descs = [describe_failure(f) for f in failures if f.strip()]
    if not descs:
        return Assertion("")
    head = descs[0]
    more = f" 외 {len(descs) - 1}건" if len(descs) > 1 else ""
    summary = head.summary if len(head.summary) + len(more) <= MAX_SUMMARY else head.summary[: MAX_SUMMARY - len(more) - 1] + "…"
    observed: list[str] = []
    for d in descs:
        observed += [v for v in d.observed if v not in observed]
    return Assertion(summary + more, head.expected, head.actual, observed)


def _result(name: str, status: str, failures: list[str]) -> CaseResult:
    msg = "\n\n".join(failures)
    a = summarize(failures) if failures else Assertion("")
    loc = _LOC.search(msg) if msg else None
    return CaseResult(name=name, status=status, message=a.summary, expected=a.expected, actual=a.actual, details=msg, observed=a.observed,
                      file=loc.group("file") if loc else "", line=int(loc.group("line")) if loc else 0)


def parse_gtest_json(path: Path) -> dict[str, CaseResult] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        return None
    out: dict[str, CaseResult] = {}
    for suite in data.get("testsuites", []):
        for t in suite.get("testsuite", []):
            name = t.get("name", "")
            failures = [_utf8(f.get("failure", "")) for f in (t.get("failures", []) or [])]
            status = "fail" if failures else "pass"
            if t.get("result") == "SKIPPED" or t.get("status") == "NOTRUN":
                status = "skip"
            out[name] = _result(name, status, failures)
    return out


def parse_gtest_stdout(text: str) -> tuple[dict[str, CaseResult], str | None]:
    """stdout에서 케이스 결과를 읽는다. 끝나지 않은 [ RUN ] 케이스(크래시 용의자)도 돌려준다."""
    out: dict[str, CaseResult] = {}
    running: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = _RUN.match(line)
        if m:
            running = m.group("case")
            buf = []
            continue
        for rx, status in ((_OK, "pass"), (_FAILED, "fail"), (_SKIPPED, "skip")):
            m = rx.match(line)
            if m and m.group("case") == running:
                msg = "\n".join(buf).strip()
                out[running] = _result(running, status, split_failures(msg) if status == "fail" and msg else [])
                running = None
                break
        else:
            if running is not None:
                buf.append(line)
    return out, running
