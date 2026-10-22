"""크래시 브리프 (설계 8.3).

커널 oops/KASAN/BUG/WARN/lockdep/hang과 유저 스페이스 ASan/UBSan/세그폴트/gdb 백트레이스를
분류하고, 책임 프레임(테스트 파일 또는 대상 소스에 속한 첫 프레임)을 찾아 가설과 함께 요약한다.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from genut.models import CrashBrief
from genut.verify.logs.ktap import strip_timestamps

KERNEL_MARKERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"BUG: kernel NULL pointer dereference"), "null_deref"),
    (re.compile(r"BUG: KASAN: (?:slab-|)use-after-free"), "kasan_uaf"),
    (re.compile(r"BUG: KASAN: \S*out-of-bounds"), "kasan_oob"),
    (re.compile(r"BUG: KASAN: double-free|BUG: KASAN: invalid-free"), "kasan_uaf"),
    (re.compile(r"kernel BUG at (?P<file>[^:]+):(?P<line>\d+)!"), "bug_on"),
    (re.compile(r"WARNING: CPU: \d+ PID: \d+ at (?P<file>[^:\s]+):(?P<line>\d+)"), "warn_on"),
    (re.compile(r"general protection fault"), "gpf"),
    (re.compile(r"BUG: unable to handle page fault for address: (?P<addr>[0-9a-fx]+)"), "page_fault"),
    (re.compile(r"possible circular locking dependency|possible recursive locking|BUG: sleeping function called from invalid context"), "lockdep"),
    (re.compile(r"UBSAN: (?P<what>.+)"), "ubsan"),
    (re.compile(r"Kernel panic - not syncing"), "panic"),
]

USER_MARKERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ERROR: AddressSanitizer: (?:heap-use-after-free|use-after-poison|double-free|attempting free)"), "asan_uaf"),
    (re.compile(r"ERROR: AddressSanitizer: (?:heap|stack|global|container)-buffer-overflow|stack-buffer-underflow"), "asan_oob"),
    (re.compile(r"ERROR: AddressSanitizer: SEGV on unknown address (?P<addr>0x[0-9a-f]+)"), "segv"),
    (re.compile(r"ERROR: LeakSanitizer"), "asan_leak"),
    (re.compile(r"(?P<file>[^\s:]+):(?P<line>\d+):\d+: runtime error: (?P<what>.+)"), "ubsan"),
    (re.compile(r"Segmentation fault|SIGSEGV"), "segv"),
    (re.compile(r"Aborted|SIGABRT|Assertion .* failed"), "abort"),
]

HYPOTHESES = {
    "null_deref": "스텁이 NULL을 반환하거나 구조체의 포인터 필드가 초기화되지 않았다. 스텁 반환값을 유효한 객체로 하거나 포인터 필드를 채운다.",
    "kasan_uaf": "해제된 객체를 사용했다. 테스트가 직접 해제했거나 스텁이 해제했다. kunit 관리 할당(kunit_kzalloc)으로 바꾼다.",
    "kasan_oob": "배열·버퍼 크기가 모자라다. 할당 크기를 구조체 cap·len과 맞춘다.",
    "bug_on": "소스가 전제 조건을 단정(BUG_ON)했는데 테스트 입력이 그 전제를 깼다. 해당 file:line의 조건을 읽고 입력을 고친다.",
    "warn_on": "소스의 WARN_ON 조건에 걸렸다. 입력이 전제를 깼는지 확인한다.",
    "gpf": "쓰레기 포인터를 따라갔다. 구조체를 전부 초기화한다.",
    "page_fault": "잘못된 주소를 접근했다. 포인터 인자·필드 초기화를 확인한다.",
    "lockdep": "락을 잡은 채 호출했거나 스텁이 락을 풀지 않았다. 스텁에 락 해제를 추가하거나 케이스를 제거한다.",
    "hang_timeout": "무한 루프나 대기에 빠졌다. 루프 종료 조건을 만족하는 입력으로 바꾸고, 안 되면 제거한다.",
    "ubsan": "정의되지 않은 동작(오버플로·시프트·정렬 등)이 일어났다. 입력 범위를 확인한다.",
    "asan_uaf": "해제된 메모리를 사용했다. 객체 수명을 케이스 안에서 맞춘다.",
    "asan_oob": "버퍼 범위를 넘었다. 버퍼 크기와 길이 인자를 맞춘다.",
    "asan_leak": "할당한 메모리를 해제하지 않았다.",
    "segv": "잘못된 메모리 접근. NULL이나 초기화되지 않은 포인터를 넘겼을 가능성이 크다.",
    "abort": "assert/abort로 중단되었다. 소스의 전제 조건을 확인한다.",
    "panic": "커널 패닉. 앞선 오류 메시지를 확인한다.",
    "unknown": "원인을 특정하지 못했다. 마지막으로 시작된 케이스의 입력을 단순하게 바꾼다.",
}

CORE_FRAMES = re.compile(
    r"^(?:dump_stack\w*|show_stack|__kasan\w*|kasan_\w+|print_report|print_address_description|"
    r"__warn\w*|report_bug|handle_bug|exc_\w+|asm_exc_\w+|do_trap|do_error_trap|__die\w*|die|oops_\w+|"
    r"page_fault\w*|do_user_addr_fault|kernelmode_fixup_or_oops|no_context|__bad_area\w*|"
    r"kunit_try_run_case\w*|kunit_generic_run_threadfn_adapter|kunit_run_case\w*|kthread|ret_from_fork\w*|"
    r"__asan_\w+|__interceptor_\w+|__libc_start\w*|_start|main|testing::\w+|RUN_ALL_TESTS|__sanitizer\w*)"
)

_TRACE_START = re.compile(r"(Call Trace:|Backtrace:|^\s*#0\s)")
_KFRAME = re.compile(r"^\s*(?P<q>\?\s*)?(?P<func>[A-Za-z_][\w.]*)\+(?P<off>0x[0-9a-f]+)/(?P<len>0x[0-9a-f]+)(?:\s+\[(?P<mod>[^\]]+)\])?(?:\s+(?P<loc>\S+:\d+))?")
_KFRAME_DECODED = re.compile(r"^\s*(?P<q>\?\s*)?(?P<func>[A-Za-z_][\w.]*)\s+\((?P<loc>[^)]+:\d+)\)")
_UFRAME = re.compile(r"^\s*#\d+\s+0x[0-9a-f]+\s+in\s+(?P<func>[^\s(]+).*?\s(?P<loc>[^\s]+:\d+)(?::\d+)?\s*$")
_GDBFRAME = re.compile(r"^#\d+\s+(?:0x[0-9a-f]+\s+in\s+)?(?P<func>[^\s(]+)\s*\(.*?\)\s+at\s+(?P<loc>\S+:\d+)")
_RIP = re.compile(r"RIP: \d+:(?P<func>[A-Za-z_][\w.]*)\+(?P<off>0x[0-9a-f]+)/(?P<len>0x[0-9a-f]+)")


@dataclass
class Frame:
    func: str
    raw: str
    location: str = ""
    unreliable: bool = False
    module: str = ""


@dataclass
class CrashContext:
    """크래시를 우리 코드와 연결할 정보."""

    test_functions: set[str] = field(default_factory=set)  # 생성 코드의 케이스 함수 이름
    target_functions: set[str] = field(default_factory=set)  # 대상 소스의 함수들
    our_files: list[str] = field(default_factory=list)  # 테스트 파일·대상 소스 경로(일부 일치)
    symbols: dict[str, str] = field(default_factory=dict)  # 이름 -> 설명 (파라미터, 스텁, 전역)
    read_source_line: Callable[[str], str] | None = None  # "file:line" -> 소스 줄
    symbolize: Callable[[str], str | None] | None = None  # "func+0x1a/0x40" -> "file:line"


def find_marker(lines: list[str], user: bool) -> tuple[int, str, re.Match[str]] | None:
    markers = USER_MARKERS if user else KERNEL_MARKERS
    for i, line in enumerate(lines):
        for rx, cat in markers:
            m = rx.search(line)
            if m:
                return i, cat, m
    return None


def parse_frames(lines: list[str]) -> list[Frame]:
    frames: list[Frame] = []
    for line in lines:
        for rx in (_KFRAME_DECODED, _KFRAME, _UFRAME, _GDBFRAME):
            m = rx.match(line)
            if m:
                gd = m.groupdict()
                func = gd["func"]
                loc = gd.get("loc") or ""
                frames.append(Frame(func=func, raw=line.strip(), location=loc, unreliable=bool(gd.get("q")), module=gd.get("mod") or ""))
                break
    return frames


def _is_ours(f: Frame, ctx: CrashContext) -> bool:
    base = f.func.split(".")[0].split("(")[0]
    if base in ctx.test_functions or base in ctx.target_functions:
        return True
    return bool(f.location) and any(p and (p in f.location or f.location.split(":")[0].endswith(p)) for p in ctx.our_files)


def build_brief(log_text: str, ctx: CrashContext, user: bool = False, suspect_case: str = "", timeout_case: str = "") -> CrashBrief | None:
    lines = strip_timestamps(log_text)
    if timeout_case:
        return CrashBrief(category="hang_timeout", suspect_case=timeout_case, hypothesis=HYPOTHESES["hang_timeout"], raw_excerpt=f"{timeout_case}: try timed out")
    hit = find_marker(lines, user)
    if hit is None:
        return None
    idx, cat, m = hit
    gd = m.groupdict()
    if cat == "page_fault":
        addr = int(gd.get("addr") or "0", 16)
        cat = "null_deref" if addr < 0x1000 else "page_fault"
    if cat == "segv" and gd.get("addr") is not None and int(gd["addr"], 16) < 0x1000:
        cat = "null_deref"
    window = lines[idx : idx + 120]
    frames = [f for f in parse_frames(window) if not f.unreliable]
    rip = next((_RIP.search(x) for x in window if _RIP.search(x)), None)
    if rip and not any(f.func == rip.group("func") for f in frames[:1]):
        frames.insert(0, Frame(func=rip.group("func"), raw=rip.group(0)))
    meaningful = [f for f in frames if not CORE_FRAMES.match(f.func)]
    ours = [f for f in meaningful if _is_ours(f, ctx)]
    blame = ours[0] if ours else (meaningful[0] if meaningful else None)
    location = ""
    if gd.get("file") and gd.get("line"):
        location = f"{gd['file']}:{gd['line']}"
    if blame is not None:
        loc = blame.location
        if not loc and ctx.symbolize is not None:
            off = re.search(r"\+0x[0-9a-f]+/0x[0-9a-f]+", blame.raw)
            if off:
                loc = ctx.symbolize(f"{blame.func}{off.group(0)}") or ""
        location = loc or location or blame.func
    source_line = ""
    if location and ":" in location and ctx.read_source_line is not None:
        source_line = ctx.read_source_line(location) or ""
    chain = []
    for f in (ours or meaningful)[:8]:
        chain.append(f"{f.func}" + (f"  ({f.location})" if f.location else ""))
    related = []
    if source_line:
        for name, desc in ctx.symbols.items():
            if re.search(rf"\b{re.escape(name)}\b", source_line):
                related.append(f"{name} ← {desc}")
    hyp = HYPOTHESES.get(cat, HYPOTHESES["unknown"])
    if cat == "null_deref" and related:
        stubs = [r for r in related if "스텁" in r]
        if stubs:
            hyp = f"{stubs[0]} 이(가) NULL을 돌려줄 수 있다. " + hyp
    excerpt_end = min(len(lines), idx + 60)
    return CrashBrief(
        category=cat,
        location=location,
        source_line=source_line,
        chain=list(reversed(chain)),
        related=related,
        hypothesis=hyp,
        suspect_case=suspect_case,
        raw_excerpt="\n".join(lines[max(0, idx - 5) : excerpt_end]),
    )


def make_source_reader(roots: list[Path]) -> Callable[[str], str]:
    def read(loc: str) -> str:
        m = re.match(r"^(?P<file>.+):(?P<line>\d+)(?::\d+)?$", loc.strip())
        if not m:
            return ""
        f, n = m.group("file"), int(m.group("line"))
        cands = [Path(f)] + [r / f for r in roots]
        for c in cands:
            try:
                if c.exists():
                    lines = c.read_text(encoding="utf-8", errors="replace").splitlines()
                    return lines[n - 1].strip() if 0 < n <= len(lines) else ""
            except OSError:
                continue
        return ""

    return read
