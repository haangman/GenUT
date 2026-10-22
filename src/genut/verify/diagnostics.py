"""컴파일러·링커 출력을 구조화된 진단으로 바꾼다 (설계 7.3).

- 같은 메시지 중복 제거, 첫 오류에서 이어진 연쇄 오류 절단, 상위 K개
- 진단을 케이스에 귀속: 어느 케이스 함수의 줄인지 찾는다 (공유 영역이면 "@shared")
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from genut.models import Diagnostic

_DIAG = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^:\n]*?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?P<sev>fatal error|error|warning|note|Error|Warning):\s*(?P<msg>.*?)\s*$"
)
_FLAG = re.compile(r"\s*\[(-W[^\]]+|-Werror[^\]]*|enabled by default)\]\s*$")
_IN_FUNC = re.compile(r"^(?P<file>.*?): In (?:member )?function [‘'`\"](?P<func>[^’'`\"]+)[’'`\"]:")
_LD_UNDEF = re.compile(r"undefined reference to [`‘'\"](?P<sym>[^’'`\"]+)[’'`\"]")
_LD_DUP = re.compile(r"multiple definition of [`‘'\"](?P<sym>[^’'`\"]+)[’'`\"]")
_LLD_UNDEF = re.compile(r"(?:ld(?:\.lld)?|lld-link|ld64\.lld): error: undefined symbol: (?P<sym>.+)$")
_LLD_DUP = re.compile(r"(?:ld(?:\.lld)?|lld-link): error: duplicate symbol: (?P<sym>.+)$")
_MSVC_UNRES = re.compile(r"unresolved external symbol\s+\"?(?P<sym>[^\s\"]+)")
_LD_FUNC = re.compile(r"in function [`‘'\"](?P<func>[^’'`\"]+)[’'`\"]")
_MODPOST = re.compile(r'ERROR: modpost: "(?P<sym>\w+)" \[(?P<mod>[^\]]+)\] undefined!')


def parse_compiler_output(text: str, stage: str = "compile") -> list[Diagnostic]:
    out: list[Diagnostic] = []
    current: Diagnostic | None = None
    func_ctx = ""
    pending_link_func = ""
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        m = _IN_FUNC.match(line)
        if m:
            func_ctx = m.group("func")
            continue
        m = _DIAG.match(line)
        if m and not line.lstrip().startswith(("make", "In file included")):
            sev = m.group("sev").lower()
            msg = m.group("msg")
            flag = ""
            fm = _FLAG.search(msg)
            if fm:
                flag = fm.group(1).lstrip("-")
                msg = msg[: fm.start()].rstrip()
            if sev == "note" and current is not None:
                current.notes.append(msg)
                continue
            # 링커가 file:line 형식으로 남기는 경우
            lm = _LD_UNDEF.search(msg) or _LD_DUP.search(msg)
            if lm:
                out.append(_link_diag(lm, msg, m.group("file"), pending_link_func or func_ctx))
                current = None
                continue
            current = Diagnostic(
                file=m.group("file").strip(),
                line=int(m.group("line")),
                col=int(m.group("col") or 0),
                severity="error" if sev == "fatal error" else sev,
                message=msg,
                flag=flag,
                stage=stage,
                owner="",
            )
            if flag.startswith("Werror"):
                current.severity = "error"
            if func_ctx:
                current.notes.append(f"in function {func_ctx}")
            out.append(current)
            continue
        fm = _LD_FUNC.search(line)
        if fm and ("undefined" not in line and "multiple definition" not in line):
            pending_link_func = fm.group("func")
            continue
        for rx in (_LD_UNDEF, _LD_DUP, _LLD_UNDEF, _LLD_DUP, _MSVC_UNRES, _MODPOST):
            lm = rx.search(line)
            if lm:
                fn = _LD_FUNC.search(line)
                out.append(_link_diag(lm, line.strip(), "", (fn.group("func") if fn else pending_link_func)))
                current = None
                break
        else:
            if current is not None and (line.startswith(" ") or line.startswith("\t") or re.match(r"^\s*\d+\s*\|", line)):
                current.snippet = (current.snippet + "\n" + line) if current.snippet else line
            elif line.startswith(">>> referenced by") and out and out[-1].stage == "link":
                out[-1].notes.append(line.strip())
    return out


def _link_diag(m: re.Match[str], msg: str, file: str, func: str) -> Diagnostic:
    sym = m.group("sym").strip()
    dup = "multiple definition" in msg or "duplicate symbol" in msg
    d = Diagnostic(
        file=file.strip(),
        severity="error",
        message=(f"multiple definition of `{sym}`" if dup else f"undefined reference to `{sym}`"),
        stage="link",
    )
    d.notes.append(f"symbol {sym}")
    if func:
        d.notes.append(f"in function {func}")
    return d


def link_symbol(d: Diagnostic) -> str:
    for n in d.notes:
        if n.startswith("symbol "):
            return n[len("symbol "):]
    m = re.search(r"`([^`]+)`", d.message)
    return m.group(1) if m else ""


def errors_only(diags: list[Diagnostic], werror: bool = False) -> list[Diagnostic]:
    out = []
    for d in diags:
        if d.severity == "error":
            out.append(d)
        elif d.severity == "warning" and werror:
            out.append(d.model_copy(update={"severity": "error", "flag": "Werror=" + (d.flag or "warning").removeprefix("W")}))
    return out


def dedupe(diags: list[Diagnostic], window: int = 2) -> list[Diagnostic]:
    """같은 위치·메시지 중복과, 첫 오류 바로 뒤 같은 줄 근처의 연쇄 오류를 지운다."""
    seen: set[tuple[str, int, str]] = set()
    kept: list[Diagnostic] = []
    last_by_file: dict[str, int] = {}
    for d in diags:
        key = (_norm(d.file), d.line, d.message)
        if key in seen:
            continue
        seen.add(key)
        if d.stage != "link" and d.file:
            prev = last_by_file.get(_norm(d.file))
            if prev is not None and 0 <= d.line - prev <= window and not d.flag:
                continue
            last_by_file[_norm(d.file)] = d.line
        kept.append(d)
    return kept


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p)) if p else ""


@dataclass
class LineMap:
    """렌더링한 테스트 파일 하나의 케이스별 줄 범위."""

    path: str
    cases: dict[str, tuple[int, int]] = field(default_factory=dict)
    function_names: dict[str, str] = field(default_factory=dict)  # 생성 코드의 함수 이름 -> 케이스 이름

    def owner_of(self, line: int) -> str:
        for name, (s, e) in self.cases.items():
            if s <= line <= e:
                return name
        return "@shared"


def attribute(diags: list[Diagnostic], maps: list[LineMap]) -> list[Diagnostic]:
    """진단마다 owner를 채운다: 케이스 이름 | "@shared" | "" (파일 밖, 예: 헤더·링크)."""
    by_path = {_norm(m.path): m for m in maps}
    names: dict[str, str] = {}
    for m in maps:
        names.update(m.function_names)
        names.update({c: c for c in m.cases})
    out = []
    for d in diags:
        owner = ""
        lm = by_path.get(_norm(d.file)) if d.file else None
        if lm is None and d.file:
            base = os.path.basename(d.file)
            lm = next((m for m in maps if os.path.basename(m.path) == base), None)
        if lm is not None and d.line:
            owner = lm.owner_of(d.line)
        else:
            for n in d.notes:
                if n.startswith("in function "):
                    fn = n[len("in function "):]
                    fn = re.sub(r"\(.*$", "", fn).split("::")[0]
                    for code_name, case in names.items():
                        if code_name == fn or fn.endswith(code_name) or code_name in fn:
                            owner = case
                            break
        out.append(d.model_copy(update={"owner": owner}))
    return out


def top_k(diags: list[Diagnostic], k: int = 5) -> list[Diagnostic]:
    return diags[:k]


def render(diags: list[Diagnostic], limit: int = 5) -> str:
    parts = []
    for d in diags[:limit]:
        s = d.short()
        if d.notes:
            s += "\n  " + "\n  ".join(n for n in d.notes[:3])
        if d.snippet:
            s += "\n" + d.snippet
        parts.append(s)
    more = f"\n... 외 {len(diags) - limit}건" if len(diags) > limit else ""
    return "\n\n".join(parts) + more
