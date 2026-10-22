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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from genut.models import CaseResult

_TS = re.compile(r"^\s*\[\s*\d+\.\d+\]\s?")
_RESULT = re.compile(r"^(?P<indent>\s*)(?P<ok>ok|not ok)\s+(?P<num>\d+)\s*-?\s*(?P<name>[^#\s]+)?(?:\s*#\s*(?P<directive>.*))?$")
_SUBTEST = re.compile(r"^(?P<indent>\s*)#\s*Subtest:\s*(?P<name>\S+)")
_FAIL = re.compile(r"^\s*#\s*(?P<case>\w+):\s*(?P<kind>EXPECTATION|ASSERTION) FAILED at (?P<file>[^:\s]+):(?P<line>\d+)")
_TIMEOUT = re.compile(r"^\s*#\s*(?P<case>\w+):\s*(?:try timed out|.*timed out)")
_DIAG = re.compile(r"^\s*#\s*(?P<case>\w+):\s*(?P<msg>.*)$")
_VALUE = re.compile(r"==\s*(?P<v>.+?)(?:\s*\((?:0x)?[0-9a-fA-F]+\))?\s*$")


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
    vals = []
    for line in block[1:]:
        m = _VALUE.search(line)
        if m:
            vals.append(m.group("v").strip())
    return vals


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
            obs = [v for b in blocks for v in _observed(b)]
            current.results[name] = CaseResult(
                name=name,
                status=status,
                message=(first.strip().lstrip("# ") if first else ("try timed out" if status == "timeout" else "")),
                details=details,
                observed=obs,
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
