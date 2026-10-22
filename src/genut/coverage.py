"""라인 커버리지 (설계 7.6): 수집 결과 읽기, 대상 함수 범위 계산, 미커버 줄의 도달 조건.

- 읽는 형식: lcov 트레이스파일(SF:/DA:/BRDA:, KUnit 스크립트·llvm-cov export), gcov JSON(gcov --json-format)
- 계산 범위: 대상 함수의 줄 범위(분석 단계에서 확정)만. 실행 가능한 줄(DA가 있는 줄)만 센다.
  #ifdef로 컴파일되지 않은 줄은 데이터에 나타나지 않으므로 자연히 빠진다(config_dependent)
- 도달 조건: 미커버 줄을 감싸는 가장 가까운 분기의 조건식과 방향, 그 조건에 등장하는 파라미터·호출(스텁
  반환값)·전역. 조건을 만족시키는 구체적인 값은 Test Writer가 정한다
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from genut.analysis.model import AnalysisReport, FunctionInfo
from genut.state import CoverageSummary

_IDENT = re.compile(r"[A-Za-z_]\w*")
_KEYWORDS = {"if", "else", "while", "for", "switch", "case", "return", "sizeof", "NULL", "true", "false", "nullptr", "참", "거짓"}


@dataclass
class FileCoverage:
    path: str
    lines: dict[int, int] = field(default_factory=dict)  # 줄 -> 실행 횟수 (실행 가능한 줄만)

    def merge(self, other: FileCoverage) -> None:
        for ln, n in other.lines.items():
            self.lines[ln] = self.lines.get(ln, 0) + n


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def parse_lcov(text: str) -> dict[str, FileCoverage]:
    out: dict[str, FileCoverage] = {}
    cur: FileCoverage | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("SF:"):
            path = line[3:]
            cur = out.setdefault(_norm(path), FileCoverage(path))
        elif line.startswith("DA:") and cur is not None:
            parts = line[3:].split(",")
            if len(parts) >= 2 and parts[0].isdigit():
                n = int(parts[1]) if parts[1].lstrip("-").isdigit() else 0
                cur.lines[int(parts[0])] = cur.lines.get(int(parts[0]), 0) + max(0, n)
        elif line == "end_of_record":
            cur = None
    return out


def parse_gcov_json(data: dict) -> dict[str, FileCoverage]:
    out: dict[str, FileCoverage] = {}
    base = data.get("current_working_directory", "")
    for f in data.get("files", []):
        path = f["file"] if Path(f["file"]).is_absolute() or not base else str(Path(base) / f["file"])
        fc = out.setdefault(_norm(path), FileCoverage(path))
        for ln in f.get("lines", []):
            fc.lines[ln["line_number"]] = fc.lines.get(ln["line_number"], 0) + int(ln.get("count", 0))
    return out


def to_lcov(files: dict[str, FileCoverage]) -> str:
    lines = []
    for fc in files.values():
        lines.append(f"SF:{fc.path}")
        lines += [f"DA:{ln},{n}" for ln, n in sorted(fc.lines.items())]
        lines.append(f"LH:{sum(1 for n in fc.lines.values() if n)}")
        lines.append(f"LF:{len(fc.lines)}")
        lines.append("end_of_record")
    return "\n".join(lines) + "\n"


def find_file(files: dict[str, FileCoverage], source: Path | str) -> FileCoverage | None:
    """경로가 같으면 그것, 아니면 파일 이름이 하나만 같은 것."""
    hit = files.get(_norm(str(source)))
    if hit is not None:
        return hit
    name = Path(str(source)).name.lower()
    same = [f for f in files.values() if Path(f.path).name.lower() == name]
    return same[0] if len(same) == 1 else None


def function_coverage(fc: FileCoverage | None, fn: FunctionInfo) -> CoverageSummary | None:
    if fc is None:
        return None
    lines = {ln: n for ln, n in fc.lines.items() if fn.start_line <= ln <= fn.end_line}
    return CoverageSummary(covered=sum(1 for n in lines.values() if n), total=len(lines), uncovered_lines=sorted(ln for ln, n in lines.items() if not n))


# ------------------------------------------------------------ 도달 조건
@dataclass
class Uncovered:
    lines: list[int]  # 같은 분기에 묶인 미커버 줄
    source: str  # 첫 줄 소스
    condition: str = ""  # "(rb_free_space(rb) < len) 참"
    branch_line: int = 0
    returns: str = ""  # 이 줄들에서 일어나는 반환
    related: list[str] = field(default_factory=list)


def _assigned_from_calls(fn: FunctionInfo) -> dict[str, str]:
    """`slot = rb_alloc_slot(...)` 같은 대입: 변수 -> 호출한 함수."""
    out = {}
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*\(", fn.source):
        out.setdefault(m.group(1), m.group(2))
    return out


def reach_conditions(report: AnalysisReport, lines: list[int]) -> list[Uncovered]:
    fn = report.function
    src = fn.source.splitlines()
    scopes = {j.name: j for j in report.linkscope}
    params = {p.name for p in fn.params}
    globs = {g.name for g in fn.globals}
    assigned = _assigned_from_calls(fn)
    groups: dict[int, Uncovered] = {}
    order: list[int] = []
    for ln in sorted(lines):
        br = fn.reach(ln)
        key = br.line if br is not None else -ln
        if key not in groups:
            text = src[ln - fn.start_line].strip() if 0 <= ln - fn.start_line < len(src) else ""
            groups[key] = Uncovered(lines=[], source=text, condition=br.condition if br else "", branch_line=br.line if br else 0)
            order.append(key)
        groups[key].lines.append(ln)
    for g in groups.values():
        rets = [r for r in fn.returns if r.line in g.lines or (g.branch_line and r.condition == g.condition)]
        if rets:
            g.returns = ", ".join(sorted({r.expr or "(void)" for r in rets}))
        seen: list[str] = []
        for name in _IDENT.findall(g.condition):
            if name in _KEYWORDS or name in seen:
                continue
            seen.append(name)
            if name in params:
                g.related.append(f"파라미터 {name}")
            elif name in scopes:
                j = scopes[name]
                g.related.append(f"{name} ({j.scope.value}{' → 스텁 반환값' if j.needs_stub else ''})")
            elif name in globs:
                g.related.append(f"전역 {name}")
            elif name in assigned:
                callee = assigned[name]
                j = scopes.get(callee)
                tag = f", {j.scope.value}" + (" → 스텁" if j.needs_stub else "") if j else ""
                g.related.append(f"{name} ← {callee}()의 반환값{tag}")
    return [groups[k] for k in order]


def render_uncovered(items: list[Uncovered]) -> str:
    out = []
    for u in items:
        where = ":" + (f"{u.lines[0]}" if len(u.lines) == 1 else f"{u.lines[0]}-{u.lines[-1]}" if u.lines == list(range(u.lines[0], u.lines[-1] + 1)) else ",".join(map(str, u.lines)))
        head = f"미커버 {where}  `{u.source}`"
        cond = f"조건 {u.condition} (:{u.branch_line})" if u.condition else "가장 가까운 분기 없음 (함수 진입 직후)"
        ret = f" → {u.returns} 반환" if u.returns else ""
        lines = [head, f"  {cond}{ret}"]
        if u.related:
            lines.append("  관련: " + ", ".join(u.related))
        out.append("\n".join(lines))
    return "\n".join(out)
