"""실패 분류와 권장 시작 전략 (설계 7.3, 7.4).

LLM은 "무슨 종류의 실패인가"를 추론하지 않고 "이 종류의 실패를 어떻게 고칠까"만 생각한다.
Fixer는 권장 시작 전략보다 아래 단계로 내려갈 수 없다.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources

import yaml

from genut.models import Diagnostic

LADDER = ["fix_case", "add_stub", "drop_case", "narrow_scope", "give_up"]


def allowed_strategies(recommended: str) -> list[str]:
    """권장 전략 이상(덜 고치고 더 포기하는 쪽)만 허용한다."""
    if recommended not in LADDER:
        return list(LADDER)
    return LADDER[LADDER.index(recommended):]


@dataclass(frozen=True)
class Classification:
    category: str
    strategy: str
    hint: str


class Classifier:
    def __init__(self, rules: dict | None = None) -> None:
        if rules is None:
            rules = yaml.safe_load((resources.files("genut.verify") / "classify.yaml").read_text(encoding="utf-8"))
        self.diag_rules: list[dict] = rules.get("diagnostic_rules", [])
        self.run_rules: dict[str, dict] = rules.get("run_rules", {})

    def classify_diagnostic(self, d: Diagnostic) -> Classification:
        msg = d.message.lower()
        for r in self.diag_rules:
            stages = r.get("stage")
            if stages and d.stage not in stages and not (d.stage == "check" and "compile" in stages):
                continue
            flags = r.get("flag")
            if flags:
                if any(d.flag.startswith(f) for f in flags):
                    return Classification(r["category"], r["strategy"], r.get("hint", ""))
                continue
            if any(p.lower() in msg for p in r.get("match", [])):
                return Classification(r["category"], r["strategy"], r.get("hint", ""))
        return Classification("compile_error", "fix_case", "")

    def classify_diagnostics(self, diags: list[Diagnostic]) -> Classification:
        """한 케이스에 귀속된 진단 묶음의 대표 분류: 첫 오류 기준, 단 링크 오류가 있으면 링크 쪽."""
        if not diags:
            return Classification("compile_error", "fix_case", "")
        link = [d for d in diags if d.stage == "link"]
        return self.classify_diagnostic(link[0] if link else diags[0])

    def classify_run(self, category: str) -> Classification:
        r = self.run_rules.get(category, {})
        return Classification(category, r.get("strategy", "fix_case"), r.get("hint", ""))
