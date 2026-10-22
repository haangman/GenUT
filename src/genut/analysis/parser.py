"""libclang 래퍼 (설계 6장)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from genut.models import Diagnostic

_SEVERITY = {0: "ignored", 1: "note", 2: "warning", 3: "error", 4: "fatal"}


class LibclangError(RuntimeError):
    pass


def load_libclang(path: str = "") -> str:
    """libclang을 불러오고 라이브러리 경로를 돌려준다."""
    try:
        import clang.cindex as ci
    except ImportError as e:  # pragma: no cover - 설치 문제
        raise LibclangError("clang 파이썬 바인딩이 없습니다 (pip install libclang).") from e
    if path and not ci.Config.loaded:
        ci.Config.set_library_file(path)
    try:
        ci.Index.create()
    except Exception as e:  # noqa: BLE001
        raise LibclangError(f"libclang 라이브러리를 불러오지 못했습니다: {e}") from e
    return str(getattr(ci.conf.lib, "_name", "libclang"))


def libclang_available() -> bool:
    try:
        load_libclang()
        return True
    except LibclangError:
        return False


@dataclass
class ParseResult:
    tu: object
    file: Path
    args: list[str]
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity in ("error", "fatal")]


def to_diagnostics(tu: object, stage: str = "check") -> list[Diagnostic]:
    out: list[Diagnostic] = []
    for d in tu.diagnostics:  # type: ignore[attr-defined]
        sev = _SEVERITY.get(d.severity, "error")
        if sev in ("ignored", "note"):
            continue
        loc = d.location
        fname = str(loc.file.name) if loc.file else ""
        notes = [c.spelling for c in d.children]
        out.append(
            Diagnostic(
                file=fname,
                line=loc.line,
                col=loc.column,
                severity=sev,
                message=d.spelling,
                flag=(d.option or "").lstrip("-"),
                notes=notes,
                stage=stage,
            )
        )
    return out


def parse(file: Path, args: list[str], unsaved: dict[str, str] | None = None, detailed: bool = False, function_bodies: bool = True) -> ParseResult:
    import clang.cindex as ci

    load_libclang()
    opts = ci.TranslationUnit.PARSE_INCOMPLETE
    if detailed:
        opts |= ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
    if not function_bodies:
        opts |= ci.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES
    files = [(k, v) for k, v in (unsaved or {}).items()]
    try:
        tu = ci.Index.create().parse(str(file), args=args, unsaved_files=files or None, options=opts)
    except ci.TranslationUnitLoadError as e:
        raise LibclangError(f"{file} 파싱 실패: {e}") from e
    return ParseResult(tu=tu, file=file, args=args, diagnostics=to_diagnostics(tu))
