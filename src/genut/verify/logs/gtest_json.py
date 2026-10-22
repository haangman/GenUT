"""gtest 결과 파서 (설계 7.5). --gtest_output=json 이 기본이고, 크래시로 JSON이 없으면 stdout으로 대신한다."""

from __future__ import annotations

import json
import re
from pathlib import Path

from genut.models import CaseResult

_RUN = re.compile(r"^\[ RUN      \] (?P<suite>[\w/]+)\.(?P<case>[\w/]+)")
_OK = re.compile(r"^\[       OK \] (?P<suite>[\w/]+)\.(?P<case>[\w/]+)")
_FAILED = re.compile(r"^\[  FAILED  \] (?P<suite>[\w/]+)\.(?P<case>[\w/]+)(?: \(\d+ ms\))?$")
_SKIPPED = re.compile(r"^\[  SKIPPED \] (?P<suite>[\w/]+)\.(?P<case>[\w/]+)")
_WHICH = re.compile(r"Which is:\s*(.+)$")
_LOC = re.compile(r"^(?P<file>(?:[A-Za-z]:)?[^:\n]+):(?P<line>\d+): Failure")


def _observed(msg: str) -> list[str]:
    vals = [m.group(1).strip() for m in (_WHICH.search(line) for line in msg.splitlines()) if m]
    for line in msg.splitlines():
        m = re.match(r"^\s*Actual:\s*(.+)$", line)
        if m:
            vals.append(m.group(1).strip())
    return vals


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
            failures = t.get("failures", []) or []
            msg = "\n\n".join(f.get("failure", "") for f in failures)
            status = "fail" if failures else "pass"
            if t.get("result") == "SKIPPED" or t.get("status") == "NOTRUN":
                status = "skip"
            loc = _LOC.search(msg) if msg else None
            out[name] = CaseResult(
                name=name,
                status=status,
                message=msg.splitlines()[0] if msg else "",
                details=msg,
                observed=_observed(msg),
                file=loc.group("file") if loc else "",
                line=int(loc.group("line")) if loc else 0,
            )
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
                loc = _LOC.search(msg) if msg else None
                out[running] = CaseResult(
                    name=running, status=status, message=msg.splitlines()[0] if msg else "", details=msg,
                    observed=_observed(msg), file=loc.group("file") if loc else "", line=int(loc.group("line")) if loc else 0,
                )
                running = None
                break
        else:
            if running is not None:
                buf.append(line)
    return out, running
