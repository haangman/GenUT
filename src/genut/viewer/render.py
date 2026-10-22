"""HTML 뷰어 (설계 11.3). 실행 기록 디렉터리만 읽어 의존성 없는 단일 HTML을 만든다.

- 상단 요약: 함수별 결과, 최종 확인, 예산(빌드·토큰·시간), provider·모델
- 타임라인: trace.jsonl의 단계·메시지·에이전트 호출·수정·롤백·격리
- 케이스 보드: 케이스별 상태 변화(new → fail → pass ...)와 본문, 마지막 실패
- 프롬프트·수정 기록·크래시 브리프·리포트 원문 (접힘)
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from genut.state import RunState

STATUS_CLASS = {"pass": "ok", "new": "info", "not_run": "info", "fail": "bad", "crash": "bad", "dropped": "muted", "suspected_bug": "warn", "flaky": "warn"}

CSS = """
:root { --bg:#fbfbfa; --fg:#1d1d1f; --muted:#6b6b70; --line:#e3e3e0; --card:#fff; --ok:#1a7f37; --bad:#c62828; --warn:#a15c00; --info:#2b5fb4; }
@media (prefers-color-scheme: dark) { :root { --bg:#151517; --fg:#e8e8ea; --muted:#9a9aa0; --line:#2c2c30; --card:#1d1d20; --ok:#4cc26a; --bad:#ff6b6b; --warn:#f0a440; --info:#79a6ff; } }
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--fg); font:14px/1.55 system-ui, -apple-system, "Segoe UI", "Malgun Gothic", sans-serif; }
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 28px 0 10px; border-bottom: 1px solid var(--line); padding-bottom: 6px; }
.sub { color: var(--muted); margin-bottom: 16px; word-break: break-all; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
.card b { display: block; font-size: 13px; color: var(--muted); font-weight: 500; }
.gauge { height: 6px; background: var(--line); border-radius: 3px; margin-top: 6px; overflow: hidden; } .gauge i { display: block; height: 100%; background: var(--info); }
table { width: 100%; border-collapse: collapse; font-size: 13px; } td, th { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 500; }
.chip { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 12px; border: 1px solid currentColor; margin: 1px 2px 1px 0; white-space: nowrap; }
.ok { color: var(--ok); } .bad { color: var(--bad); } .warn { color: var(--warn); } .info { color: var(--info); } .muted { color: var(--muted); }
pre { background: var(--card); border: 1px solid var(--line); border-radius: 6px; padding: 10px; overflow-x: auto; font: 12px/1.45 ui-monospace, Consolas, monospace; white-space: pre-wrap; word-break: break-word; }
details { margin: 6px 0; } summary { cursor: pointer; } .case { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px; margin: 8px 0; }
.t { color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
"""


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def _events(run: Path) -> list[dict]:
    text = _read(run / "trace.jsonl")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _summary(state: RunState | None, meta: dict) -> str:
    b = meta.get("budget", {})
    used, cap = b.get("builds_used", 0), b.get("builds_max", 0) or 1
    tokens = sum((b.get("tokens") or {}).values())
    cards = [
        ("provider · 모델", f"{meta.get('provider', '-')} · {meta.get('model', '-')}"),
        ("빌드", f"{used} / {cap}<div class='gauge'><i style='width:{min(100, 100 * used // cap)}%'></i></div>"),
        ("토큰", f"{tokens:,}"),
        ("소요", f"{b.get('elapsed_sec', '-')}s"),
    ]
    out = "<div class='cards'>" + "".join(f"<div class='card'><b>{_e(k)}</b>{v}</div>" for k, v in cards) + "</div>"
    if state is not None:
        rows = []
        for fs in state.functions.values():
            res = fs.result or "-"
            cls = {"complete": "ok", "partial": "warn", "none": "bad"}.get(res, "muted")
            counts: dict[str, int] = {}
            for c in fs.cases:
                counts[c.status.value] = counts.get(c.status.value, 0) + 1
            cnt = " ".join(f"<span class='chip {STATUS_CLASS.get(k, '')}'>{_e(k)} {v}</span>" for k, v in sorted(counts.items()))
            rows.append(f"<tr><td><b>{_e(fs.name)}</b><div class='muted'>{_e(fs.signature)}</div></td><td class='{cls}'>{_e(res)}</td>"
                        f"<td>{_e(fs.final_check or '-')}</td><td>{cnt}</td><td>{_e(fs.status.value)} {_e(fs.status_reason)}</td></tr>")
        out += "<h2>함수</h2><table><tr><th>함수</th><th>결과</th><th>최종 확인</th><th>케이스</th><th>상태</th></tr>" + "".join(rows) + "</table>"
        if state.notes:
            out += "<h2>참고</h2><ul>" + "".join(f"<li>{_e(n)}</li>" for n in state.notes) + "</ul>"
    return out


def _timeline(events: list[dict]) -> str:
    if not events:
        return "<p class='muted'>trace.jsonl이 없습니다.</p>"
    t0 = events[0].get("ts", 0)
    rows = []
    for e in events:
        typ = e.get("type", "")
        if typ == "stage_start":
            text = f"<b>단계 시작: {_e(e.get('stage'))}</b>"
        elif typ == "stage_end":
            text = f"<span class='muted'>단계 끝: {_e(e.get('stage'))} ({_e(e.get('seconds'))}s)</span>"
        elif typ == "message":
            text = _e(e.get("text", ""))
        elif typ == "llm_call":
            trims = e.get("trims") or []
            trim = f" · 잘림 {len(trims)}개" if trims else ""
            err = f" <span class='bad'>오류 {_e(e['error'])}</span>" if e.get("error") else ""
            text = (f"<span class='info'>LLM</span> {_e(e.get('agent', ''))} · {_e(e.get('function', ''))} · 토큰 {_e(e.get('tokens', '-'))}/{_e(e.get('limit', '-'))}"
                    f"{trim}{err} · <a href='#{_e(e.get('prompt_file', ''))}'>{_e(e.get('prompt_file', ''))}</a>")
        elif typ == "fix":
            text = (f"<span class='warn'>수정</span> {_e(e.get('case'))}: {_e(e.get('category'))} → 권장 {_e(e.get('recommended'))}, "
                    f"선택 {_e(e.get('chosen'))}, 적용 {_e(e.get('applied'))}")
        else:
            data = {k: v for k, v in e.items() if k not in ("ts", "type")}
            text = f"<span class='muted'>{_e(typ)}</span> {_e(json.dumps(data, ensure_ascii=False)[:300])}"
        rows.append(f"<tr><td class='t'>+{e.get('ts', t0) - t0:.1f}s</td><td>{text}</td></tr>")
    return "<table>" + "".join(rows) + "</table>"


def _cases(state: RunState | None) -> str:
    if state is None:
        return "<p class='muted'>state.json이 없습니다.</p>"
    out = []
    for fs in state.functions.values():
        out.append(f"<h3>{_e(fs.name)}</h3>")
        for c in fs.cases:
            hist = " → ".join(f"<span class='chip {STATUS_CLASS.get(h.status, '')}' title='{_e(h.note)}'>r{h.round} {_e(h.stage)}:{_e(h.status)}</span>" for h in c.history)
            fail = ""
            if c.last_failure is not None:
                fail = f"<details><summary>마지막 실패: {_e(c.last_failure.category)} — {_e(c.last_failure.summary[:120])}</summary><pre>{_e(c.last_failure.model_dump_json(indent=1))}</pre></details>"
            reason = f"<div class='muted'>{_e(c.drop_reason)}</div>" if c.drop_reason else ""
            prev = f"<details><summary>직전 본문</summary><pre>{_e(c.previous_body)}</pre></details>" if c.previous_body else ""
            out.append(f"<div class='case'><b class='{STATUS_CLASS.get(c.status.value, '')}'>{_e(c.name)}</b> <span class='chip {STATUS_CLASS.get(c.status.value, '')}'>{_e(c.status.value)}</span>"
                       f" <span class='muted'>{_e(c.origin)} · 수정 {c.fixes_used}회 · {_e(c.purpose)}</span>{reason}<div>{hist}</div>"
                       f"<details><summary>본문</summary><pre>{_e(c.body)}</pre></details>{prev}{fail}</div>")
    return "".join(out)


def _files(run: Path, sub: str, title: str) -> str:
    d = run / sub
    files = sorted(p for p in d.rglob("*") if p.is_file()) if d.is_dir() else []
    if not files:
        return ""
    items = "".join(f"<details id='{_e(p.relative_to(run).as_posix())}'><summary>{_e(p.relative_to(run).as_posix())}</summary><pre>{_e(_read(p))}</pre></details>" for p in files)
    return f"<h2>{_e(title)}</h2>{items}"


def render_run(run: Path) -> str:
    sp = run / "state.json"
    state = RunState.load(sp) if sp.exists() else None
    meta = json.loads(_read(run / "run.json") or "{}")
    title = f"GenUT 실행 {run.name}"
    sub = " · ".join(x for x in (state.source_file if state else "", meta.get("backend", ""), "중단됨" if state and state.interrupted else "") if x)
    report = _read(run / "final" / "report.md")
    body = (
        f"<h1>{_e(title)}</h1><div class='sub'>{_e(sub)}</div>{_summary(state, meta)}"
        f"<h2>타임라인</h2>{_timeline(_events(run))}<h2>케이스 보드</h2>{_cases(state)}"
        + _files(run, "prompts", "프롬프트 (시스템 + 유저 + 응답 원문)")
        + _files(run, "fixes", "수정 기록")
        + _files(run, "crash", "크래시 브리프")
        + (f"<h2>리포트</h2><pre>{_e(report)}</pre>" if report else "")
    )
    return f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>{_e(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>\n"


def write_view(run: Path) -> Path:
    out = run / "view.html"
    out.write_text(render_run(run), encoding="utf-8")
    return out
