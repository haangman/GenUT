"""호출 스택 심볼화 (설계 8.3): func+0x1a/0x40 → file:line.

커널은 scripts/faddr2line vmlinux func+off/len 을, 유저 스페이스는 addr2line을 쓴다.
디버그 정보가 없거나 도구가 없으면 None (함수 이름까지만 쓴다).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

_FADDR = re.compile(r"\bat\s+(?P<file>[^\s:]+):(?P<line>\d+)")


def faddr2line(kernel_src: Path, vmlinux: Path, frame: str, timeout: int = 30) -> str | None:
    script = kernel_src / "scripts" / "faddr2line"
    if not script.exists() or not vmlinux.exists():
        return None
    try:
        r = subprocess.run([str(script), str(vmlinux), frame], capture_output=True, text=True, timeout=timeout, cwd=str(kernel_src))
    except (OSError, subprocess.SubprocessError):
        return None
    m = _FADDR.search(r.stdout)
    return f"{m.group('file')}:{m.group('line')}" if m else None


def addr2line(binary: Path, address: str, timeout: int = 15) -> str | None:
    exe = shutil.which("addr2line") or shutil.which("llvm-addr2line")
    if not exe or not binary.exists():
        return None
    try:
        r = subprocess.run([exe, "-e", str(binary), address], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return None if not out or out.startswith("??") else out.split(" ")[0]
