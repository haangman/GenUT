"""TOML 읽기(표준 라이브러리)와 단순한 쓰기.

쓰기는 GenUT가 만드는 설정 파일(products.toml, product.toml)에만 쓰므로
문자열·숫자·불리언·문자열 목록·중첩 테이블만 지원한다.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


def load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        esc = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{esc}"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_fmt_value(x) for x in v) + "]"
    raise TypeError(f"지원하지 않는 TOML 값: {type(v).__name__}")


def _key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() else _fmt_value(k)


def dumps_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []

    def emit(table: dict[str, Any], prefix: str) -> None:
        scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
        subs = {k: v for k, v in table.items() if isinstance(v, dict)}
        if prefix and (scalars or not subs):
            lines.append(f"[{prefix}]")
        for k, v in scalars.items():
            if v is None:
                continue
            lines.append(f"{_key(k)} = {_fmt_value(v)}")
        if prefix and scalars:
            lines.append("")
        for k, v in subs.items():
            emit(v, f"{prefix}.{_key(k)}" if prefix else _key(k))

    emit(data, "")
    return "\n".join(lines).rstrip() + "\n"


def write_toml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps_toml(data), encoding="utf-8")
