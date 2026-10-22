"""로그 축약기 (설계 8.2): 토큰 상한 안에서 우선순위대로 조립한다.

1 우리 스위트 결과 (항상 유지) → 2 기대값 실패 블록 → 3 크래시 브리프 → 4 크래시 직전 꼬리.
넘치면 4 → 3 → 2 순으로 줄인다. 반복 줄은 접고, 레지스터·Code: 바이트 덤프·타임스탬프는 뺀다.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from genut.llm.tokens import estimate_tokens

NOISE = re.compile(
    r"^\s*(?:Code: |R[A-Z0-9]{2}: [0-9a-f]{16}|[A-Z]{2,3}: [0-9a-f]{4,}|CR[0-4]: |DR[0-7]: |FS: |GS: |ES: |CS: |DS: |"
    r"Modules linked in:|Hardware name:|<TASK>|</TASK>|---\[ end trace|PKRU:|EFLAGS:|RSP: |RAX: |RDX: |RBP: |R10: |R13: )"
)


def fold_repeats(lines: list[str]) -> list[str]:
    out: list[str] = []
    prev, count = None, 0
    for line in lines:
        if line == prev:
            count += 1
            continue
        if count:
            out.append(f"[... 위 줄 {count}회 반복]")
        out.append(line)
        prev, count = line, 0
    if count:
        out.append(f"[... 위 줄 {count}회 반복]")
    return out


def clean(lines: list[str]) -> list[str]:
    return fold_repeats([line for line in lines if not NOISE.match(line)])


@dataclass
class Part:
    priority: int
    title: str
    text: str

    def render(self) -> str:
        return f"### {self.title}\n{self.text}" if self.text else ""


def assemble(parts: list[Part], budget: int, count: Callable[[str], int] = estimate_tokens) -> str:
    """priority 1은 항상 넣고, 넘치면 높은 번호부터 앞부분만 남기거나 뺀다."""
    parts = sorted(parts, key=lambda p: p.priority)

    def total() -> int:
        return sum(count(p.render()) for p in parts)

    for p in sorted(parts, key=lambda p: -p.priority):
        if p.priority == 1 or total() <= budget:
            continue
        over = total() - budget
        cur = count(p.render())
        keep_tokens = max(0, cur - over)
        if keep_tokens < 40:
            p.text = ""
        else:
            ratio = keep_tokens / max(cur, 1)
            keep = max(1, int(len(p.text) * ratio * 0.9))
            tail = p.title.startswith("크래시 직전")
            p.text = (p.text[-keep:] if tail else p.text[:keep]) + "\n[... 축약됨]"
    return "\n\n".join(r for p in parts if (r := p.render()))


def tail_before(lines: list[str], index: int, n: int = 30) -> str:
    return "\n".join(clean(lines[max(0, index - n) : index]))
