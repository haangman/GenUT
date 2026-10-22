"""규칙 md 로드와 겹쳐 올리기 (설계 5장, 11.5).

공통 규칙($GENUT_HOME/rules/) 뒤에 프로덕트 규칙(products/<이름>/rules/)을 덧붙인다.
프롬프트에는 두 묶음을 구분해 넣는다. 아래 제목은 코드도 읽는다.

- "반드시 스텁": 백틱 안의 이름·glob 패턴은 정의가 있어도 스텁 대상 (6.4 must_stub)
- "리뷰에서 거부할 패턴": 백틱 안의 정규식은 코드 리뷰 검사에서 거부 (7.9)
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TICK = re.compile(r"`([^`\n]+)`")


def strip_comments(md: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", _COMMENT.sub("", md)).strip()


def sections(md: str) -> dict[str, str]:
    """'## 제목' 단위로 나눈다. 같은 제목이 여러 번 나오면 이어 붙인다."""
    out: dict[str, str] = {}
    current = ""
    for line in md.splitlines():
        m = re.match(r"^#{2,3}\s+(.*\S)\s*$", line)
        if m:
            current = m.group(1)
            out.setdefault(current, "")
            continue
        if current:
            out[current] += line + "\n"
    return out


def _ticks_in(md: str, keywords: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for title, body in sections(strip_comments(md)).items():
        if any(k in title.lower() for k in keywords):
            found += [t.strip() for t in _TICK.findall(body)]
    return found


@dataclass
class Rules:
    common_text: str = ""
    product_text: str = ""
    files: list[Path] = field(default_factory=list)
    must_stub: list[str] = field(default_factory=list)
    reject_patterns: list[str] = field(default_factory=list)

    def prompt_block(self) -> str:
        parts = []
        if self.common_text:
            parts.append("## 공통 규칙 (반드시 지킬 것)\n\n" + self.common_text)
        if self.product_text:
            parts.append("## 이 프로덕트 규칙 (공통 규칙과 충돌하면 이쪽이 우선)\n\n" + self.product_text)
        return "\n\n".join(parts)

    def is_must_stub(self, symbol: str) -> bool:
        return any(fnmatch.fnmatchcase(symbol, pat) for pat in self.must_stub)

    def compiled_rejects(self) -> list[re.Pattern[str]]:
        out = []
        for pat in self.reject_patterns:
            try:
                out.append(re.compile(pat))
            except re.error:
                out.append(re.compile(re.escape(pat)))
        return out


def _read_set(folder: Path, backend: str) -> tuple[str, list[Path]]:
    texts, files = [], []
    for name in ("common.md", f"{backend}.md"):
        p = folder / name
        if p.exists():
            texts.append(strip_comments(p.read_text(encoding="utf-8")))
            files.append(p)
    return "\n\n".join(t for t in texts if t), files


def load_rules(home: Path, product_dir: Path | None, backend: str) -> Rules:
    common, cfiles = _read_set(home / "rules", backend)
    product, pfiles = ("", []) if product_dir is None else _read_set(product_dir / "rules", backend)
    raw = "\n\n".join(p.read_text(encoding="utf-8") for p in cfiles + pfiles)
    stub_kw = ("반드시 스텁", "must stub")
    reject_kw = ("리뷰에서 거부", "reject")
    return Rules(
        common_text=common,
        product_text=product,
        files=cfiles + pfiles,
        must_stub=_ticks_in(raw, stub_kw),
        reject_patterns=_ticks_in(raw, reject_kw),
    )
