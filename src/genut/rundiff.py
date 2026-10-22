"""genut diff: 두 실행 비교 (설계 11.1, 11.4 튜닝 루프).

튜닝은 "무엇을 바꿨더니 결과가 어떻게 달라졌나"를 보는 일이므로 두 가지를 나란히 보여 준다.
- 바뀐 입력: 설정(run.json의 config), 규칙 md 해시, 역할 프롬프트 해시, provider·모델, 대상 함수 본문(analysis/<키>.json)
- 바뀐 결과: 함수 결과·커버리지·최종 확인, 케이스 상태와 상태 경로(new → fail → pass), 빌드·토큰·LLM 호출·수정 횟수,
  수정한 실패 분류 분포
실행 기록 디렉터리만 읽는다(state.json, run.json, trace.jsonl, analysis/). LLM·빌드는 부르지 않는다.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from genut.state import CaseStatus, FunctionState, RunState, TestCase, text_hash

MAX_CONFIG_LINES = 30
_LABEL = {"pass": "통과", "dropped": "제거", "suspected_bug": "소스 버그 의심", "flaky": "불안정", "not_run": "미실행", "fail": "실패",
          "crash": "크래시", "new": "미검증"}


@dataclass
class RunRecord:
    path: Path
    state: RunState
    meta: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)  # 함수 키 -> 분석한 함수 본문 해시

    @property
    def run_id(self) -> str:
        return self.state.run_id


def load_run(path: Path) -> RunRecord:
    sp = path / "state.json"
    if not sp.exists():
        raise FileNotFoundError(f"{path}: state.json이 없습니다 (실행이 시작 전에 멈췄을 수 있음).")
    rec = RunRecord(path=path, state=RunState.load(sp))
    rj = path / "run.json"
    if rj.exists():
        rec.meta = json.loads(rj.read_text(encoding="utf-8"))
    tj = path / "trace.jsonl"
    if tj.exists():
        for line in tj.read_text(encoding="utf-8").splitlines():
            try:
                rec.events.append(json.loads(line))
            except json.JSONDecodeError:  # 중단으로 마지막 줄이 잘렸을 수 있다
                continue
    for key in rec.state.functions:
        aj = path / "analysis" / f"{key}.json"
        if aj.exists():
            src = json.loads(aj.read_text(encoding="utf-8")).get("function", {}).get("source", "")
            rec.sources[key] = text_hash(src)
    return rec


# ------------------------------------------------------------------ 입력
def flatten(d: Any, prefix: str = "") -> dict[str, Any]:
    """중첩 dict를 a.b.c 키로 편다. 목록은 값 그대로 둔다."""
    if not isinstance(d, dict):
        return {prefix: d}
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            out.update(flatten(v, key))
        else:
            out[key] = v
    return out


def config_changes(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    fa, fb = flatten(a), flatten(b)
    return [{"key": k, "a": fa.get(k), "b": fb.get(k)} for k in sorted(set(fa) | set(fb)) if fa.get(k) != fb.get(k)]


def input_changes(a: RunRecord, b: RunRecord) -> dict[str, Any]:
    ma, mb = a.meta, b.meta
    pa, pb = ma.get("prompts", {}), mb.get("prompts", {})
    out: dict[str, Any] = {
        "config": config_changes(ma.get("config", {}), mb.get("config", {})),
        "rules_changed": bool(ma.get("rules_hash")) and bool(mb.get("rules_hash")) and ma.get("rules_hash") != mb.get("rules_hash"),
        "prompts_changed": sorted(r for r in set(pa) & set(pb) if pa[r] != pb[r]),
        "source_changed": sorted(k for k in set(a.sources) & set(b.sources) if a.sources[k] != b.sources[k]),
    }
    for k in ("provider", "model"):
        if ma.get(k) != mb.get(k):
            out[k] = [ma.get(k, ""), mb.get(k, "")]
    return out


# ------------------------------------------------------------------ 결과
def metrics(r: RunRecord) -> dict[str, Any]:
    fns = list(r.state.functions.values())
    cases = [c for fs in fns for c in fs.cases]
    calls = [e for e in r.events if e.get("type") == "llm_call"]
    budget = r.meta.get("budget", {})
    tokens = sum(budget.get("tokens", {}).values()) if budget else sum(fs.tokens_used for fs in fns)
    return {
        "results": dict(Counter(fs.result or "-" for fs in fns)),
        "cases": len(cases),
        "statuses": dict(Counter(c.status.value for c in cases)),
        "builds": budget.get("builds_used", r.state.builds_used),
        "tokens": tokens,
        "seconds": budget.get("elapsed_sec", r.state.seconds_used),
        "llm_calls": dict(Counter(e.get("agent") or e.get("role", "") for e in calls)),
        "fixes": dict(Counter(e.get("category", "") for e in r.events if e.get("type") == "fix")),
    }


def status_path(c: TestCase) -> str:
    """상태 경로. 같은 상태가 이어지면 하나로 줄인다: new → fail → pass."""
    seq: list[str] = []
    for e in c.history:
        if not seq or seq[-1] != e.status:
            seq.append(e.status)
    return " → ".join(seq) or c.status.value


def _case(c: TestCase | None) -> dict[str, Any] | None:
    if c is None:
        return None
    return {"status": c.status.value, "path": status_path(c), "fixes": c.fixes_used, "category": c.last_failure.category if c.last_failure else "",
            "reason": c.drop_reason, "body": c.body_hash()}


def _function(fs: FunctionState | None) -> dict[str, Any] | None:
    if fs is None:
        return None
    cov = fs.coverage
    return {"result": fs.result, "status": fs.status.value, "reason": fs.status_reason, "passed": len(fs.passed()), "cases": len(fs.cases),
            "coverage": round(cov.percent, 1) if cov and cov.total else None, "final_check": fs.final_check}


def function_changes(a: RunRecord, b: RunRecord) -> list[dict[str, Any]]:
    out = []
    for key in list(a.state.functions) + [k for k in b.state.functions if k not in a.state.functions]:
        fa, fb = a.state.functions.get(key), b.state.functions.get(key)
        ca = {c.name: c for c in fa.cases} if fa else {}
        cb = {c.name: c for c in fb.cases} if fb else {}
        cases, same = [], 0
        for name in list(ca) + [n for n in cb if n not in ca]:
            xa, xb = _case(ca.get(name)), _case(cb.get(name))
            if xa and xb and xa["status"] == xb["status"] and xa["fixes"] == xb["fixes"] and xa["body"] == xb["body"]:
                same += 1
                continue
            cases.append({"name": name, "a": xa, "b": xb, "body_changed": bool(xa and xb and xa["body"] != xb["body"])})
        out.append({"name": key, "a": _function(fa), "b": _function(fb), "cases": cases, "unchanged": same})
    return out


def diff_runs(a: RunRecord, b: RunRecord) -> dict[str, Any]:
    warnings = []
    if Path(a.state.source_file).resolve() != Path(b.state.source_file).resolve():
        warnings.append(f"대상 파일이 다릅니다: {a.state.source_file} / {b.state.source_file}")
    if a.state.backend != b.state.backend:
        warnings.append(f"백엔드가 다릅니다: {a.state.backend} / {b.state.backend}")
    for r in (a, b):
        if not r.state.finished:
            warnings.append(f"{r.run_id}: 끝나지 않은 실행입니다" + (" (중단됨)" if r.state.interrupted else f" (단계 {r.state.stage})"))
    relation = ""
    if b.meta.get("replay_of") == a.run_id:
        relation = "b_replays_a"
    elif a.meta.get("replay_of") == b.run_id:
        relation = "a_replays_b"
    return {
        "a": {"run_id": a.run_id, "path": str(a.path), "source_file": a.state.source_file, "backend": a.state.backend},
        "b": {"run_id": b.run_id, "path": str(b.path), "source_file": b.state.source_file, "backend": b.state.backend},
        "relation": relation,
        "replay_live_from": (b.meta if relation == "b_replays_a" else a.meta).get("replay_live_from", "") if relation else "",
        "warnings": warnings,
        "inputs": input_changes(a, b),
        "metrics": {"a": metrics(a), "b": metrics(b)},
        "functions": function_changes(a, b),
    }


# ------------------------------------------------------------------ 출력
def _arrow(x: Any, y: Any, unit: str = "") -> str:
    if x == y:
        return f"{x}{unit} (같음)"
    if isinstance(x, (int, float)) and isinstance(y, (int, float)) and not isinstance(x, bool):
        d = y - x
        d_txt = f"{d:+.1f}" if isinstance(d, float) else f"{d:+d}"
        return f"{x}{unit} → {y}{unit} ({d_txt})"
    return f"{x}{unit} → {y}{unit}"


def _counts(a: dict[str, int], b: dict[str, int], label: dict[str, str] | None = None) -> str:
    """키별 개수. 같으면 한 번만, 다르면 a → b."""
    parts = []
    for k in sorted(set(a) | set(b)):
        name, x, y = (label or {}).get(k, k) or "(없음)", a.get(k, 0), b.get(k, 0)
        parts.append(f"{name} {x}" if x == y else f"{name} {x} → {y}")
    return ", ".join(parts) or "(없음)"


def _short(v: Any) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else repr(v)
    return s if len(s) <= 60 else s[:57] + "..."


def _fn_line(name: str, fa: dict | None, fb: dict | None) -> str:
    if fa is None:
        return f"{name}: B에만 있음 ({fb['result'] or fb['status']})"
    if fb is None:
        return f"{name}: A에만 있음 ({fa['result'] or fa['status']})"
    parts = [f"{fa['result'] or '-'} → {fb['result'] or '-'}" if fa["result"] != fb["result"] else f"{fb['result'] or '-'}"]
    parts.append(f"통과 {fa['passed']}/{fa['cases']} → {fb['passed']}/{fb['cases']}" if (fa["passed"], fa["cases"]) != (fb["passed"], fb["cases"])
                 else f"통과 {fb['passed']}/{fb['cases']}")
    if fa["coverage"] is not None or fb["coverage"] is not None:
        ca = "-" if fa["coverage"] is None else f"{fa['coverage']:g}%"
        cb = "-" if fb["coverage"] is None else f"{fb['coverage']:g}%"
        parts.append(f"커버리지 {ca} → {cb}" if ca != cb else f"커버리지 {cb}")
    if fa["final_check"] != fb["final_check"]:
        parts.append(f"최종 확인 {fa['final_check'] or '-'} → {fb['final_check'] or '-'}")
    if fa["status"] != fb["status"]:
        parts.append(f"상태 {fa['status']} → {fb['status']}" + (f" ({fb['reason']})" if fb["reason"] else ""))
    return f"{name}: " + " · ".join(parts)


def _case_line(c: dict[str, Any]) -> str:
    a, b = c["a"], c["b"]

    def tag(x: dict) -> str:
        return x["path"] + (f" [{x['category']}]" if x["category"] and x["status"] != CaseStatus.PASS.value else "")

    if a is None:
        return f"{c['name']}: B에만 있음 ({_LABEL.get(b['status'], b['status'])}, {tag(b)})"
    if b is None:
        return f"{c['name']}: A에만 있음 ({_LABEL.get(a['status'], a['status'])}, {tag(a)})"
    head = f"{_LABEL.get(a['status'], a['status'])} → {_LABEL.get(b['status'], b['status'])}" if a["status"] != b["status"] else _LABEL.get(b["status"], b["status"])
    extra = []
    if a["fixes"] != b["fixes"]:
        extra.append(f"수정 {a['fixes']}회 → {b['fixes']}회")
    if c["body_changed"]:
        extra.append("본문 다름")
    return f"{c['name']}: {head} (A: {tag(a)} / B: {tag(b)})" + (" · " + " · ".join(extra) if extra else "")


def render_diff(d: dict[str, Any]) -> str:
    a, b = d["a"], d["b"]
    lines = [f"실행 비교: A {a['run_id']} → B {b['run_id']}", f"  대상: {b['source_file']} ({b['backend']})"]
    if d["relation"] == "b_replays_a":
        lines.append("  B는 A의 replay입니다" + (f" (--from {d['replay_live_from']}부터 실제 LLM)" if d["replay_live_from"] else " (기록된 응답만)"))
    elif d["relation"] == "a_replays_b":
        lines.append("  A는 B의 replay입니다")
    lines += [f"  경고: {w}" for w in d["warnings"]]

    inp = d["inputs"]
    lines += ["", "바뀐 입력"]
    changed = []
    for k in ("provider", "model"):
        if k in inp:
            changed.append(f"  {k}: {inp[k][0] or '-'} → {inp[k][1] or '-'}")
    for c in inp["config"][:MAX_CONFIG_LINES]:
        changed.append(f"  설정 {c['key']}: {_short(c['a'])} → {_short(c['b'])}")
    if len(inp["config"]) > MAX_CONFIG_LINES:
        changed.append(f"  설정 외 {len(inp['config']) - MAX_CONFIG_LINES}개 (--json으로 전부 볼 수 있음)")
    if inp["rules_changed"]:
        changed.append("  규칙 md: 바뀜")
    if inp["prompts_changed"]:
        changed.append("  역할 프롬프트: " + ", ".join(inp["prompts_changed"]))
    if inp["source_changed"]:
        changed.append("  대상 함수 본문: " + ", ".join(inp["source_changed"]))
    lines += changed or ["  없음 (같은 설정·규칙·프롬프트·대상 함수)"]

    ma, mb = d["metrics"]["a"], d["metrics"]["b"]
    sa, sb = ma["statuses"], mb["statuses"]
    lines += [
        "",
        "결과",
        "  함수 " + _counts(ma["results"], mb["results"]),
        f"  케이스 통과 {sa.get('pass', 0)}/{ma['cases']} → {sb.get('pass', 0)}/{mb['cases']}"
        + "".join(f" · {_LABEL[s]} {sa.get(s, 0)} → {sb.get(s, 0)}" for s in ("dropped", "suspected_bug", "flaky") if sa.get(s) or sb.get(s)),
        f"  빌드 {_arrow(ma['builds'], mb['builds'])} · 토큰 {_arrow(ma['tokens'], mb['tokens'])} · 소요 {_arrow(ma['seconds'], mb['seconds'], 's')}",
        f"  LLM 호출 {_arrow(sum(ma['llm_calls'].values()), sum(mb['llm_calls'].values()))}: " + _counts(ma["llm_calls"], mb["llm_calls"]),
        "  수정한 실패 분류: " + _counts(ma["fixes"], mb["fixes"]),
    ]

    lines += ["", "함수별"]
    for f in d["functions"]:
        lines.append("  " + _fn_line(f["name"], f["a"], f["b"]))
        lines += ["    " + _case_line(c) for c in f["cases"]]
        if f["unchanged"]:
            lines.append(f"    그대로: {f['unchanged']}개")
    return "\n".join(lines)
