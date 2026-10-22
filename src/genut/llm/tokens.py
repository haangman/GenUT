"""토큰 세기 (설계 9.7).

Gemma 4 토크나이저를 로컬에 둘 수 없는 환경을 고려해 기본은 보수적 추정이다.
추정치는 항상 실제보다 크게 잡는다. genut doctor가 실제 usage와 비교해 보정 계수를
$GENUT_HOME/knowledge/token_calibration.json에 남기면 이후 추정에 곱한다.
"""

from __future__ import annotations

import json
import math
from pathlib import Path


def _is_hangul(ch: str) -> bool:
    o = ord(ch)
    return 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F


def estimate_tokens(text: str) -> int:
    """영문·코드는 3자당 1토큰, 한글은 글자당 1.5토큰, 그 밖의 비ASCII는 글자당 1토큰.

    영문은 실제로 4자당 1토큰 정도라 일부러 크게 잡는다(코드와 구분하지 않으려고).
    """
    ascii_chars = hangul = other = 0
    for ch in text:
        if ord(ch) < 128:
            ascii_chars += 1
        elif _is_hangul(ch):
            hangul += 1
        else:
            other += 1
    return math.ceil(ascii_chars / 3 + hangul * 1.5 + other)


class TokenCounter:
    def __init__(self, calibration: float = 1.0, exact=None) -> None:  # noqa: ANN001
        self.calibration = max(1.0, calibration)
        self._exact = exact  # callable(str) -> int | None

    def __call__(self, text: str) -> int:
        if self._exact is not None:
            n = self._exact(text)
            if n is not None:
                return n
        return math.ceil(estimate_tokens(text) * self.calibration)


def calibration_path(home: Path) -> Path:
    return home / "knowledge" / "token_calibration.json"


def load_calibration(home: Path, model: str) -> float:
    p = calibration_path(home)
    if not p.exists():
        return 1.0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return float(data.get(model, 1.0))
    except (ValueError, OSError):
        return 1.0


def save_calibration(home: Path, model: str, estimated: int, actual: int) -> float:
    """실제/추정 비율을 기록한다. 추정이 이미 크면 1.0(보정 없음)."""
    factor = max(1.0, actual / estimated) if estimated > 0 else 1.0
    p = calibration_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    data[model] = round(factor * 1.05, 3) if factor > 1.0 else 1.0
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data[model]
