"""GENUT_HOME과 프로덕트 폴더 경로 (설계 11.5)."""

from __future__ import annotations

import os
from pathlib import Path


def genut_home() -> Path:
    """공통 저장소 위치. 환경 변수 GENUT_HOME이 없으면 ~/.genut."""
    env = os.environ.get("GENUT_HOME")
    return Path(env).expanduser() if env else Path.home() / ".genut"


def product_dir(name: str, home: Path | None = None) -> Path:
    return (home or genut_home()) / "products" / name


def ensure_home_layout(home: Path) -> None:
    """공통 저장소의 기본 디렉터리를 만든다."""
    for sub in ("rules", "prompts", "stub-tools", "knowledge", "evals/baselines", "products"):
        (home / sub).mkdir(parents=True, exist_ok=True)


def ensure_product_layout(pdir: Path) -> None:
    """프로덕트 폴더의 기본 디렉터리를 만든다."""
    for sub in ("rules", "stub-tools", "knowledge", "runs", "evals/corpus"):
        (pdir / sub).mkdir(parents=True, exist_ok=True)


def is_relative_to(path: Path, parent: Path) -> bool:
    """대소문자를 무시하는 플랫폼(Windows)도 고려한 하위 경로 판정."""
    p = os.path.normcase(str(path.resolve()))
    q = os.path.normcase(str(parent.resolve()))
    return p == q or p.startswith(q.rstrip("\\/") + os.sep)
