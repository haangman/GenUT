"""단계별 검증의 1단계: libclang 의미 검사 (설계 7.2).

빌드 없이 같은 플래그로 테스트 파일을 파싱해 초 단위로 흔한 실수를 잡는다.
컴파일 DB에 -Werror가 있거나 커널이 CONFIG_WERROR=y면 경고도 오류로 본다.
프레임워크·시스템 헤더(gtest/gtest.h 등)를 찾지 못하면 이 환경에서는 의미 검사를 할 수 없으므로
"unavailable"로 알리고 빌드 단계에 맡긴다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from genut.analysis.parser import LibclangError, parse, to_diagnostics
from genut.models import Diagnostic
from genut.verify.diagnostics import dedupe, errors_only

FRAMEWORK_HEADERS = re.compile(r"'(gtest/[^']+|gmock/[^']+|kunit/[^']+|linux/[^']+|asm/[^']+|std\w*\.h|string\.h|cstd\w*|c?stdlib\.h?|stdio\.h|vector|string|memory|iostream)' file not found")


@dataclass
class CheckResult:
    available: bool
    diagnostics: list[Diagnostic] = field(default_factory=list)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.available or not self.diagnostics


def werror_enabled(args: list[str], dotconfig_text: str = "") -> bool:
    if "-Werror" in args:
        return True
    return bool(re.search(r"^CONFIG_WERROR=y\s*$", dotconfig_text, re.M))


def semantic_check(path: Path, text: str, args: list[str], werror: bool = False, unsaved: dict[str, str] | None = None) -> CheckResult:
    files = dict(unsaved or {})
    files[str(path)] = text
    try:
        pr = parse(path, args, unsaved=files)
    except LibclangError as e:
        return CheckResult(False, reason=str(e))
    # 헤더를 못 찾으면 libclang은 fatal로 낸다. 빌드 로그 파서처럼(fatal error → error) 오류로 맞춘다.
    diags = [d.model_copy(update={"severity": "error"}) if d.severity == "fatal" else d for d in to_diagnostics(pr.tu, stage="check")]
    for d in diags:
        if d.severity == "error" and FRAMEWORK_HEADERS.search(d.message):
            return CheckResult(False, reason=f"의미 검사 불가(프레임워크 헤더 없음): {d.message}")
    return CheckResult(True, dedupe(errors_only(diags, werror)))


def cpp_args_from_c(args: list[str], std: str = "c++17") -> list[str]:
    """C 소스의 플래그를 C++ 테스트용으로 바꾼다 (-std=gnu11 → -std=c++17, C 전용 경고 제거)."""
    out = []
    for a in args:
        if a.startswith("-std="):
            continue
        if a in ("-Wstrict-prototypes", "-Wmissing-prototypes", "-Wold-style-definition", "-Wimplicit-function-declaration", "-Wdeclaration-after-statement"):
            continue
        out.append(a)
    return ["-x", "c++", f"-std={std}"] + out
