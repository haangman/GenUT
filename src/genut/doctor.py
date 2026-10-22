"""genut doctor: 환경 점검 (설계 11.1).

점검 하나는 (상태, 제목, 설명, 해결 방법)을 낸다. 상태는 ok | warn | fail.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from genut.context import AppContext


@dataclass
class Check:
    status: str
    title: str
    detail: str = ""
    fix: str = ""


def _libclang() -> Check:
    try:
        from genut.analysis.parser import load_libclang

        path = load_libclang()
        return Check("ok", "libclang", str(path))
    except Exception as e:  # noqa: BLE001
        return Check("fail", "libclang", str(e), "pip install libclang 으로 설치하거나 [analysis] libclang_path를 지정하세요.")


def _provider(ctx: AppContext) -> list[Check]:
    llm = ctx.config.llm
    out: list[Check] = []
    if llm.provider == "anthropic":
        env = llm.anthropic.api_key_env
        out.append(
            Check("ok", "LLM 키", f"{env} 설정됨")
            if os.environ.get(env)
            else Check("warn", "LLM 키", f"환경 변수 {env}가 없습니다.", f"{env}에 API 키를 넣으세요.")
        )
        root = ctx.config.product.root
        if root and "sample" not in root.replace("\\", "/").lower() and "fixtures" not in root.lower():
            out.append(
                Check(
                    "warn",
                    "데이터 정책",
                    "provider가 anthropic(외부 API)인데 프로덕트가 샘플 코퍼스가 아닙니다.",
                    "실제 프로덕트 코드는 사내 Gemma 4(openai_compat)로만 처리하세요 (설계 9.1).",
                )
            )
    elif llm.provider == "openai_compat":
        oc = llm.openai_compat
        out.append(Check("ok", "LLM 엔드포인트", f"{oc.base_url} · {oc.model} · 컨텍스트 {oc.max_context}"))
        if oc.api_key_env and not os.environ.get(oc.api_key_env):
            out.append(Check("warn", "LLM 키", f"환경 변수 {oc.api_key_env}가 없습니다.", "키가 필요 없는 서버면 api_key_env를 비우세요."))
    else:
        out.append(Check("ok", "LLM", "fake provider (테스트용)"))
    return out


def _gtest_checks(ctx: AppContext, root: Path) -> list[Check]:
    g = ctx.config.gtest
    out: list[Check] = []
    exe = shutil.which(g.cmake_exe)
    out.append(Check("ok", "cmake", exe) if exe else Check("fail", "cmake", f"{g.cmake_exe}를 찾을 수 없습니다.", "CMake를 설치하거나 [gtest] cmake_exe를 지정하세요."))
    src = root / g.cmake_source_dir / "CMakeLists.txt"
    out.append(Check("ok", "CMakeLists.txt", str(src)) if src.exists() else Check("fail", "CMakeLists.txt", f"{src}가 없습니다.", "[gtest] cmake_source_dir를 확인하세요."))
    if not g.link_targets:
        out.append(
            Check(
                "warn",
                "link_targets",
                "비어 있어 CMake File API로 대상 소스가 속한 타깃만 테스트마다 링크합니다.",
                "대상 소스가 다른 라이브러리의 함수를 부르면 [gtest] link_targets에 그 타깃까지 적으세요.",
            )
        )
    if g.crash_backtrace == "gdb" and not shutil.which("gdb"):
        out.append(Check("warn", "gdb", "gdb가 없어 새니타이저 없는 크래시는 종료 코드로만 분류합니다.", "gdb를 설치하거나 [gtest] sanitizers를 켜세요 (설계 8.3)."))
    lists_dir = (root / g.cmake_lists_dir).resolve()
    if src.exists():
        text = src.read_text(encoding="utf-8", errors="replace")
        rel = g.cmake_lists_dir.replace("\\", "/").strip("/")
        if rel not in text:
            out.append(
                Check(
                    "warn",
                    "add_subdirectory",
                    f"루트 CMakeLists.txt에 {rel}가 연결되어 있지 않습니다. GenUT 빌드는 주입 방식으로 동작합니다.",
                    f"사용자 빌드에서도 쓰려면 루트 CMakeLists.txt에 add_subdirectory({rel})를 한 줄 추가하세요.",
                )
            )
    out.append(Check("ok", "생성 위치", f"CMakeLists: {lists_dir} · 테스트: {(root / g.output_dir).resolve()}"))
    return out


def _kunit_checks(ctx: AppContext) -> list[Check]:
    k = ctx.config.kunit
    out: list[Check] = []
    ksrc = Path(k.kernel_src) if k.kernel_src else None
    if not ksrc or not ksrc.is_dir():
        out.append(Check("fail", "kernel_src", f"커널 소스 경로가 없습니다: {k.kernel_src!r}", "[kunit] kernel_src를 지정하세요."))
        return out
    cdb = Path(k.compile_commands) if k.compile_commands else ksrc / "compile_commands.json"
    out.append(
        Check("ok", "compile_commands.json", str(cdb))
        if cdb.exists()
        else Check("fail", "compile_commands.json", f"{cdb}가 없습니다.", "커널 트리에서 make compile_commands.json 을 실행하세요.")
    )
    script = Path(k.run_script) if k.run_script else None
    if script and not script.is_absolute():
        script = ksrc / script
    out.append(
        Check("ok", "실행 스크립트", str(script))
        if script and script.exists()
        else Check("fail", "실행 스크립트", f"{k.run_script!r}를 찾을 수 없습니다.", "[kunit] run_script에 계약을 따르는 스크립트를 지정하세요 (docs/kunit-script.md).")
    )
    if not k.test_file:
        out.append(Check("fail", "집합 파일", "[kunit] test_file이 비어 있습니다.", "Makefile/Kconfig에 등록한 집합 파일 경로를 적으세요."))
    else:
        from genut.backends.kunit.kbuild import registration_of

        reg = registration_of(ksrc, k.test_file)
        if reg is None:
            out.append(
                Check(
                    "fail",
                    "집합 파일 등록",
                    f"{k.test_file}가 같은 디렉터리 Makefile에 등록되어 있지 않습니다.",
                    "obj-$(CONFIG_..._KUNIT_TEST) += <이름>.o 줄과 Kconfig 항목을 먼저 추가하세요.",
                )
            )
        else:
            out.append(Check("ok", "집합 파일 등록", f"{reg.kind} ({reg.line.strip()})"))
            dotconfig = ksrc / ".config"
            if reg.config_symbol and dotconfig.exists():
                text = dotconfig.read_text(encoding="utf-8", errors="replace")
                if f"{reg.config_symbol}=y" not in text and f"{reg.config_symbol}=m" not in text:
                    out.append(Check("fail", "Kconfig", f"{reg.config_symbol}가 .config에서 꺼져 있습니다.", f"{reg.config_symbol}=y 로 켜세요."))
            if dotconfig.exists():
                text = dotconfig.read_text(encoding="utf-8", errors="replace")
                for sym, why in (("CONFIG_KASAN", "메모리 오류 원인이 로그에 남음"), ("CONFIG_DEBUG_INFO", "호출 스택을 file:line으로 복원")):
                    if f"{sym}=y" not in text:
                        out.append(Check("warn", sym, f"{sym}가 꺼져 있습니다 ({why}).", f"{sym}=y 를 권장합니다 (설계 8.5)."))
                if ctx.config.coverage.enabled and "CONFIG_GCOV_KERNEL=y" not in text:
                    out.append(Check("warn", "CONFIG_GCOV_KERNEL", "커널 커버리지가 꺼져 있어 라인 커버리지를 잴 수 없습니다.", "CONFIG_GCOV_KERNEL=y 와 GCOV_PROFILE_<obj> := y 를 설정하세요."))
    if not k.test_dir:
        out.append(Check("warn", "test_dir", "함수별 테스트 파일 위치가 비어 있어 집합 파일과 같은 디렉터리를 씁니다."))
    return out


def collect_checks(ctx: AppContext) -> list[Check]:
    checks: list[Check] = []
    home = ctx.home
    checks.append(Check("ok", "GENUT_HOME", str(home)) if home.exists() else Check("fail", "GENUT_HOME", f"{home}가 없습니다.", "genut init 을 실행하세요."))
    for name in ("common.md", "gtest.md", "kunit.md"):
        p = home / "rules" / name
        if not p.exists():
            checks.append(Check("warn", f"규칙 {name}", f"{p}가 없습니다.", "genut init 으로 템플릿을 만드세요."))
    checks.append(Check("ok", "설정 출처", " < ".join(ctx.sources)))
    checks.append(_libclang())
    checks += _provider(ctx)
    if ctx.product is None:
        checks.append(Check("warn", "프로덕트", "현재 디렉터리에서 프로덕트를 찾지 못했습니다.", "genut product add 로 등록하거나 --product 를 주세요."))
        return checks
    p = ctx.product
    checks.append(Check("ok", "프로덕트", f"{p.name} · {ctx.config.backend} · {p.root}"))
    if not p.root.is_dir():
        checks.append(Check("fail", "소스 트리", f"{p.root}가 없습니다."))
        return checks
    from genut.rules import load_rules

    rules = load_rules(ctx.home, p.dir, ctx.config.backend)
    checks.append(Check("ok", "로드되는 규칙", ", ".join(str(f) for f in rules.files) or "(없음)"))
    from genut.knowledge import KnowledgeStore

    ks = KnowledgeStore(ctx.home, p.dir)
    checks.append(Check("ok", "로드되는 지식", f"공통 {len(ks.notes('global'))}개 · 프로덕트 {len(ks.notes('product'))}개"))
    if ctx.config.backend == "gtest":
        checks += _gtest_checks(ctx, p.root)
    else:
        checks += _kunit_checks(ctx)
    return checks


def run_doctor(ctx: AppContext, echo: Callable[[str], None]) -> bool:
    mark = {"ok": "[OK]  ", "warn": "[경고]", "fail": "[실패]"}
    checks = collect_checks(ctx)
    for c in checks:
        echo(f"{mark[c.status]} {c.title}: {c.detail}")
        if c.fix and c.status != "ok":
            echo(f"        해결: {c.fix}")
    failed = [c for c in checks if c.status == "fail"]
    echo("")
    echo("문제 없음" if not failed else f"실패 {len(failed)}건. 위의 해결 방법을 따르세요.")
    return not failed
