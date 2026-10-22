"""Kbuild Makefile 읽기: 집합 파일 등록 확인과 built-in/모듈 판정 (설계 6.4, 11.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_OBJ = re.compile(r"^\s*obj-(y|m|\$\((CONFIG_\w+)\))\s*[+:]?=\s*(.*)$")
_COMPOSITE = re.compile(r"^\s*([\w-]+)-(objs|y|m|\$\((CONFIG_\w+)\))\s*[+:]?=\s*(.*)$")


@dataclass
class Registration:
    kind: str  # built-in | module | conditional
    config_symbol: str | None
    line: str
    makefile: Path
    object_name: str


def _logical_lines(text: str) -> list[str]:
    out, buf = [], ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append(buf + line)
        buf = ""
    if buf:
        out.append(buf)
    return out


def _makefile_for(directory: Path) -> Path | None:
    for name in ("Kbuild", "Makefile"):
        p = directory / name
        if p.exists():
            return p
    return None


def _match_obj(lines: list[str], obj: str) -> tuple[str, str | None, str] | None:
    for line in lines:
        m = _OBJ.match(line)
        if m and obj in m.group(3).split():
            if m.group(1) == "y":
                return "built-in", None, line
            if m.group(1) == "m":
                return "module", None, line
            return "conditional", m.group(2), line
    return None


def registration_of(kernel_src: Path, test_file: str) -> Registration | None:
    """test_file(커널 트리 기준 경로)이 같은 디렉터리 Makefile에 등록되어 있는지 찾는다."""
    path = (kernel_src / test_file) if not Path(test_file).is_absolute() else Path(test_file)
    mk = _makefile_for(path.parent)
    if mk is None:
        return None
    lines = _logical_lines(mk.read_text(encoding="utf-8", errors="replace"))
    obj = path.with_suffix(".o").name
    hit = _match_obj(lines, obj)
    if hit:
        return Registration(hit[0], hit[1], hit[2], mk, obj)
    # 복합 오브젝트: foo-objs := a.o foo_kunit.o  /  obj-$(CONFIG_FOO) += foo.o
    for line in lines:
        m = _COMPOSITE.match(line)
        if m and obj in m.group(4).split() and not line.lstrip().startswith("obj-"):
            hit = _match_obj(lines, f"{m.group(1)}.o")
            if hit:
                return Registration(hit[0], hit[1], hit[2], mk, f"{m.group(1)}.o")
    return None


def effective_kind(reg: Registration, dotconfig_text: str) -> str:
    """conditional이면 .config로 built-in/module/off를 정한다."""
    if reg.kind != "conditional" or not reg.config_symbol:
        return reg.kind
    if re.search(rf"^{reg.config_symbol}=y\s*$", dotconfig_text, re.M):
        return "built-in"
    if re.search(rf"^{reg.config_symbol}=m\s*$", dotconfig_text, re.M):
        return "module"
    return "off"


def file_is_built(kernel_src: Path, source: Path, dotconfig_text: str) -> bool:
    """소스 파일이 현재 .config에서 빌드되는지 (obj-$(CONFIG_X)가 꺼져 있으면 False)."""
    try:
        rel = source.resolve().relative_to(kernel_src.resolve())
    except ValueError:
        return True
    reg = registration_of(kernel_src, str(rel))
    if reg is None:
        return True  # 판정 불가면 빌드된다고 본다
    return effective_kind(reg, dotconfig_text) != "off"
