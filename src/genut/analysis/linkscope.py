"""링크 범위 판정 (설계 6.4).

호출 심볼마다 same_tu | linkable | external | undefined | must_stub 로 판정한다.
- gtest: 테스트가 링크하는 사용자 타깃(link_targets)의 소스에 정의가 있으면 linkable
- KUnit: 테스트가 built-in이면 vmlinux에 들어가는 파일의 정의는 linkable, 모듈이면 EXPORT_SYMBOL이 필요
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from genut.analysis.model import CallSite, FunctionInfo, Scope, SymbolJudgment
from genut.rules import Rules

_DEF_TMPL = r"^[^\S\n]*(?:[A-Za-z_][\w\s\*]*?[\s\*])?{name}\s*\([^;{{}}]*\)\s*(?:__\w+(?:\(\([^)]*\)\))?\s*)*\{{"
_EXPORT = re.compile(r"\bEXPORT_SYMBOL(?:_GPL|_NS|_NS_GPL)?\s*\(\s*(\w+)")


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def _under(path: str, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


class SourceIndex:
    """소스 파일 목록에서 함수 정의를 정규식으로 찾는다 (파일별 캐시)."""

    def __init__(self, sources: list[Path]) -> None:
        self.sources = [Path(s) for s in sources]
        self._text: dict[str, str] = {}

    def _read(self, p: Path) -> str:
        k = str(p)
        if k not in self._text:
            try:
                self._text[k] = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                self._text[k] = ""
        return self._text[k]

    def find_definition(self, name: str) -> Path | None:
        pat = re.compile(_DEF_TMPL.format(name=re.escape(name)), re.M)
        for s in self.sources:
            text = self._read(s)
            if name in text and pat.search(text):
                return s
        return None


@dataclass
class GTestLinkContext:
    product_root: Path
    link_sources: list[Path] = field(default_factory=list)
    system_dirs: list[Path] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.index = SourceIndex(self.link_sources)


@dataclass
class KernelLinkContext:
    kernel_src: Path
    test_kind: str = "built-in"  # built-in | module
    built_sources: list[Path] = field(default_factory=list)  # compile DB에 있는 = 현재 .config로 빌드되는 파일
    cache_path: Path | None = None
    _exports: set[str] | None = None

    def __post_init__(self) -> None:
        self.index = SourceIndex([])

    def exported(self) -> set[str]:
        if self._exports is not None:
            return self._exports
        key = str(len(self.built_sources))
        if self.cache_path and self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if data.get("key") == key:
                    self._exports = set(data.get("exports", []))
                    return self._exports
            except ValueError:
                pass
        found: set[str] = set()
        for s in self.built_sources:
            try:
                text = s.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "EXPORT_SYMBOL" in text:
                found.update(_EXPORT.findall(text))
        self._exports = found
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({"key": key, "exports": sorted(found)}), encoding="utf-8")
        return found

    def same_dir_sources(self, source: Path) -> list[Path]:
        d = _norm(source.parent)
        return [s for s in self.built_sources if _norm(s.parent) == d and _norm(s) != _norm(source)]


def _builtin(name: str) -> bool:
    return name.startswith("__builtin") or name.startswith("__sync_") or name.startswith("__atomic_")


def judge_call(call: CallSite, source: Path, backend: str, rules: Rules, ctx: GTestLinkContext | KernelLinkContext) -> SymbolJudgment:
    name = call.name
    if rules.is_must_stub(name):
        return SymbolJudgment(name=name, scope=Scope.MUST_STUB, reason="규칙 md의 반드시 스텁 패턴", is_static=call.is_static)
    if _builtin(name):
        return SymbolJudgment(name=name, scope=Scope.EXTERNAL, reason="컴파일러 내장 함수")
    if call.def_file:
        where = "대상 소스" if _norm(call.def_file) == _norm(source) else ("헤더 인라인" if call.is_inline else "포함된 파일")
        return SymbolJudgment(name=name, scope=Scope.SAME_TU, reason=f"이 번역 단위에 정의 있음 ({where})", evidence=call.def_file, is_static=call.is_static)
    if isinstance(ctx, GTestLinkContext):
        hit = ctx.index.find_definition(name)
        if hit is not None:
            return SymbolJudgment(name=name, scope=Scope.LINKABLE, reason="링크하는 타깃의 소스에 정의", evidence=str(hit))
        decl = call.decl_file
        if decl and (not _under(decl, ctx.product_root) or any(_under(decl, d) for d in ctx.system_dirs)):
            return SymbolJudgment(name=name, scope=Scope.EXTERNAL, reason="시스템·외부 라이브러리 선언", evidence=decl)
        return SymbolJudgment(name=name, scope=Scope.UNDEFINED, reason="링크하는 타깃에서 정의를 찾지 못함", evidence=decl)
    # KUnit
    exported = name in ctx.exported()
    local = SourceIndex(ctx.same_dir_sources(source)).find_definition(name)
    if local is not None and (ctx.test_kind == "built-in" or exported):
        return SymbolJudgment(name=name, scope=Scope.LINKABLE, reason="같은 디렉터리의 빌드되는 파일에 정의", evidence=str(local))
    if exported:
        return SymbolJudgment(name=name, scope=Scope.LINKABLE, reason="EXPORT_SYMBOL로 내보내짐")
    if ctx.test_kind == "module":
        return SymbolJudgment(name=name, scope=Scope.UNDEFINED, reason="테스트가 모듈인데 심볼이 내보내지지 않음", evidence=call.decl_file)
    decl = call.decl_file
    core = decl and _under(decl, ctx.kernel_src) and re.search(r"[\\/](include|arch[\\/][^\\/]+[\\/]include)[\\/]", decl)
    if core:
        return SymbolJudgment(name=name, scope=Scope.EXTERNAL, reason="커널 공개 헤더의 선언 (vmlinux에 있다고 가정)", evidence=decl)
    return SymbolJudgment(name=name, scope=Scope.UNDEFINED, reason="빌드되는 파일에서 정의를 찾지 못함", evidence=decl)


def judge_calls(fn: FunctionInfo, source: Path, backend: str, rules: Rules, ctx: GTestLinkContext | KernelLinkContext) -> list[SymbolJudgment]:
    out: dict[str, SymbolJudgment] = {}
    for call in fn.calls:
        if call.indirect or call.name in out:
            continue
        out[call.name] = judge_call(call, source, backend, rules, ctx)
    return list(out.values())
