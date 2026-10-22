"""compile_commands.json 로더 (설계 6.2). gtest·KUnit 공통.

- 상대 경로 -I/-include 는 항목의 directory 기준 절대 경로로 바꾼다 (libclang은 프로세스 cwd 기준으로 읽음)
- 새로 만들 테스트 파일은 DB에 없으므로 대상 소스와 같은 디렉터리(없으면 가장 가까운 상위)의 항목 플래그를 빌린다
- DB가 없으면 커널은 .<name>.o.cmd 를 직접 읽는다
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_DROP_WITH_VALUE = {"-o", "-MF", "-MT", "-MQ", "-Xclang-debug"}
_DROP_SINGLE = {"-c", "-M", "-MM", "-MD", "-MMD", "-MP", "-S", "-E", "-pipe"}
_PATH_WITH_VALUE = {"-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros", "-isysroot", "--sysroot"}
_SOURCE_EXT = (".c", ".cc", ".cpp", ".cxx", ".C", ".c++", ".m", ".S", ".s")


def _norm(p: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


@dataclass
class CompileEntry:
    file: Path
    directory: Path
    arguments: list[str]  # 컴파일러 포함 원본
    compiler: str = ""

    def clean_args(self) -> list[str]:
        """libclang에 넘길 인자: 컴파일러·입력 파일·출력·의존성 옵션 제거, 경로 절대화."""
        args = self.arguments[1:]
        out: list[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            if a in _DROP_WITH_VALUE:
                i += 2
                continue
            if a in _DROP_SINGLE or a.startswith("-Wp,-M") or a.startswith("-MF") or a.startswith("-o"):
                i += 1
                continue
            if not a.startswith("-") and a.endswith(_SOURCE_EXT):
                i += 1
                continue
            if a in _PATH_WITH_VALUE and i + 1 < len(args):
                out += [a, self._abs(args[i + 1])]
                i += 2
                continue
            for flag in ("-I", "-isystem", "-iquote"):
                if a.startswith(flag) and len(a) > len(flag) and not a.startswith(flag + "-"):
                    out.append(flag + self._abs(a[len(flag):]))
                    break
            else:
                out.append(a)
            i += 1
        return out

    def _abs(self, p: str) -> str:
        return str(Path(p)) if os.path.isabs(p) else str((self.directory / p).resolve())

    def include_dirs(self) -> list[Path]:
        out = []
        args = self.clean_args()
        for i, a in enumerate(args):
            if a in ("-I", "-isystem", "-iquote") and i + 1 < len(args):
                out.append(Path(args[i + 1]))
            elif a.startswith(("-I", "-isystem", "-iquote")) and len(a) > 2:
                for flag in ("-isystem", "-iquote", "-I"):
                    if a.startswith(flag):
                        out.append(Path(a[len(flag):]))
                        break
        return out


def _split(command: str) -> list[str]:
    posix = os.name != "nt" or "\\" not in command
    try:
        return shlex.split(command, posix=posix)
    except ValueError:
        return command.split()


@dataclass
class CompileDB:
    path: Path | None
    entries: list[CompileEntry] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> CompileDB:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = []
        for e in data:
            directory = Path(e.get("directory", path.parent))
            args = e.get("arguments") or _split(e.get("command", ""))
            if not args:
                continue
            f = Path(e["file"])
            if not f.is_absolute():
                f = (directory / f).resolve()
            entries.append(CompileEntry(file=f, directory=directory, arguments=list(args), compiler=args[0]))
        return cls(path=path, entries=entries)

    def find(self, file: Path) -> CompileEntry | None:
        key = _norm(file.resolve())
        for e in self.entries:
            if _norm(e.file) == key:
                return e
        return None

    def neighbor(self, file: Path) -> CompileEntry | None:
        """같은 디렉터리 항목, 없으면 가장 깊이 공유하는 상위 디렉터리의 항목."""
        target = _norm(file.resolve().parent)
        best, best_len = None, -1
        for e in self.entries:
            d = _norm(e.file.parent)
            if d == target:
                return e
            common = os.path.commonpath([d, target]) if os.path.splitdrive(d)[0] == os.path.splitdrive(target)[0] else ""
            if len(common) > best_len:
                best, best_len = e, len(common)
        return best

    def files(self) -> list[Path]:
        return [e.file for e in self.entries]


def load_kernel_cmd(source: Path, kernel_src: Path) -> CompileEntry | None:
    """커널 빌드가 남긴 .<name>.o.cmd 에서 컴파일 명령을 읽는다 (compile_commands.json이 없을 때)."""
    cmd = source.parent / f".{source.stem}.o.cmd"
    if not cmd.exists():
        return None
    text = cmd.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^(?:savedcmd|cmd)_\S+\s*:=\s*(.*)$", text, re.M)
    if not m:
        return None
    args = _split(m.group(1))
    if ";" in args:
        args = args[: args.index(";")]
    return CompileEntry(file=source.resolve(), directory=kernel_src, arguments=args, compiler=args[0] if args else "")


def guess_target(entry: CompileEntry, kernel: bool) -> str | None:
    """libclang 대상 트리플. 커널은 Linux 트리플이 필요하다(Windows 호스트 기본값이면 long 크기가 다름)."""
    if any(a.startswith("--target=") or a == "-target" for a in entry.arguments):
        return None
    m = re.match(r"(?:.*[\\/])?([a-z0-9_]+-[a-z0-9_]+-[a-z0-9_]+(?:-[a-z0-9_]+)?)-(?:gcc|g\+\+|cc|c\+\+|clang|clang\+\+)(?:-[\d.]+)?(?:\.exe)?$", entry.compiler)
    if m:
        return m.group(1)
    if kernel:
        arch = "aarch64" if any("arm64" in a or "aarch64" in a for a in entry.arguments) else "x86_64"
        return f"{arch}-linux-gnu"
    return None


_BUILTIN_CACHE: dict[tuple[str, str], list[str]] = {}
_MACHINE_CACHE: dict[str, str] = {}
_SEARCH = re.compile(r"#include <\.\.\.> search starts here:\n(.*?)\nEnd of search list", re.S)


def compiler_search_dirs(exe: str, lang: str) -> list[str]:
    """컴파일러의 기본 #include <...> 검색 경로 (`<cc> -E -v -x <lang> -`).

    CMake는 이 경로를 컴파일 명령에 적지 않는다. libclang은 모르므로 넘겨야 한다.
    gcc 내장 디렉터리만 넘기면 그 안의 #include_next(MinGW의 stddef.h 등)가 깨진다.
    """
    try:
        r = subprocess.run([exe, "-E", "-v", "-x", lang, "-"], input="", capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    m = _SEARCH.search(r.stderr.replace("\r\n", "\n"))
    if not m:
        return []
    dirs = [line.strip().removesuffix(" (framework directory)") for line in m.group(1).splitlines() if line.strip()]
    return [os.path.normpath(d) for d in dirs if Path(d).is_dir()]


def compiler_target(entry: CompileEntry) -> str | None:
    """Windows의 GNU 툴체인(MinGW)이면 그 트리플. libclang의 Windows 기본값(MSVC)으로는 MinGW 헤더를 읽지 못한다."""
    exe = shutil.which(entry.compiler) if entry.compiler else None
    if not exe:
        return None
    if exe not in _MACHINE_CACHE:
        try:
            r = subprocess.run([exe, "-dumpmachine"], capture_output=True, text=True, timeout=10)
            _MACHINE_CACHE[exe] = r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            _MACHINE_CACHE[exe] = ""
    machine = _MACHINE_CACHE[exe]
    return machine if "mingw" in machine else None


def builtin_include_args(entry: CompileEntry, configured: str = "") -> list[str]:
    """컴파일러 기본 헤더(stddef.h, C++ 표준 라이브러리, 시스템 include) 위치. pip의 libclang에는 이 헤더가 없다.

    설정값 > 컴파일러의 기본 검색 경로 전체(-E -v) > -print-file-name=include (gcc) > clang -print-resource-dir 순.
    -nostdinc를 쓰는 빌드(커널)는 자기 헤더를 쓰므로 건드리지 않는다.
    """
    if configured:
        p = Path(configured)
        return ["-resource-dir", str(p)] if (p / "include").is_dir() else ["-isystem", str(p)]
    if "-nostdinc" in entry.arguments:
        return []
    lang = "c" if entry.file.suffix == ".c" else "c++"
    key = (entry.compiler, lang)
    if key in _BUILTIN_CACHE:
        return _BUILTIN_CACHE[key]
    out: list[str] = []
    exe = shutil.which(entry.compiler) if entry.compiler else None
    if exe:
        out = [x for d in compiler_search_dirs(exe, lang) for x in ("-isystem", d)]
    if exe and not out:
        try:
            r = subprocess.run([exe, "-print-file-name=include"], capture_output=True, text=True, timeout=10)
            path = Path(r.stdout.strip())
            if r.returncode == 0 and (path / "stddef.h").exists():
                out = ["-isystem", str(path)]
        except (OSError, subprocess.SubprocessError):
            pass
    if not out:
        clang = shutil.which("clang")
        if clang:
            try:
                r = subprocess.run([clang, "-print-resource-dir"], capture_output=True, text=True, timeout=10)
                path = Path(r.stdout.strip())
                if r.returncode == 0 and (path / "include" / "stddef.h").exists():
                    out = ["-resource-dir", str(path)]
            except (OSError, subprocess.SubprocessError):
                pass
    _BUILTIN_CACHE[key] = out
    return out
