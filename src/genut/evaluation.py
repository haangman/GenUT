"""genut eval: 같은 코퍼스로 성공률을 재고 규칙·프롬프트·모델 변경의 효과를 숫자로 비교한다 (설계 11.4, 13.2).

코퍼스는 프로덕트의 evals/corpus/corpus.toml(또는 --corpus):

    [[item]]
    file = "src/ring_buffer.c"   # 프로덕트 루트 기준
    function = "rb_push"         # 생략하면 파일의 모든 비static 함수

항목마다 genut gen과 같은 실행을 하되 프로덕트 트리에는 설치하지 않는다(측정만). 실행 기록은 평소처럼 runs/에 남고
평가 결과는 products/<이름>/evals/runs/<시각>-<라벨>/에 results.json, report.md, report.html로 쓴다.
모델별 기준치는 $GENUT_HOME/evals/baselines/<모델>.json에 저장해 다음 평가와 비교한다. 결과는 complete와 partial을
따로 세어 "일부라도 건진 비율"도 본다(7.10). 실행 기록의 규칙 md 해시와 프롬프트 해시로 무엇이 바뀌었는지 추적한다.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from genut.backends.base import Backend
from genut.context import AppContext
from genut.llm.provider import ChatProvider
from genut.orchestrator import GenOptions, Orchestrator
from genut.state import CaseStatus, FunctionState

RAN = {"pass", "crash", "not_run", "timeout"}  # 이 상태를 한 번이라도 거쳤으면 빌드된 케이스


class CorpusItem(BaseModel):
    file: str
    function: str = ""


class FunctionMetrics(BaseModel):
    function: str
    result: str
    final_check: str
    written: int
    built: int
    passed: int
    dropped: int
    suspected_bugs: int
    flaky: int
    coverage: float | None = None
    fixes: int = 0
    tokens: int = 0
    failure_categories: dict[str, int] = Field(default_factory=dict)


class ItemResult(BaseModel):
    file: str
    function: str = ""
    run_id: str = ""
    error: str = ""
    builds: int = 0
    iterations: int = 0
    seconds: float = 0.0
    functions: list[FunctionMetrics] = Field(default_factory=list)


class EvalSummary(BaseModel):
    provider: str = ""
    model: str = ""
    items: int = 0
    functions: int = 0
    errors: int = 0
    complete_rate: float = 0.0
    salvaged_rate: float = 0.0  # complete + partial: 통과 케이스가 하나라도 있는 함수
    none_rate: float = 0.0
    case_build_rate: float = 0.0
    case_pass_rate: float = 0.0
    avg_coverage: float | None = None
    avg_builds: float = 0.0
    avg_iterations: float = 0.0
    avg_seconds: float = 0.0
    avg_tokens: float = 0.0  # 함수당
    suspected_bugs: int = 0
    failure_categories: dict[str, int] = Field(default_factory=dict)
    rules_hash: str = ""
    prompts: dict[str, str] = Field(default_factory=dict)


class EvalResult(BaseModel):
    label: str = ""
    started: str = ""
    corpus: str = ""
    summary: EvalSummary = Field(default_factory=EvalSummary)
    items: list[ItemResult] = Field(default_factory=list)


# ------------------------------------------------------------ 코퍼스
def corpus_path(ctx: AppContext) -> Path:
    return ctx.product.dir / "evals" / "corpus" / "corpus.toml"


def load_corpus(path: Path) -> list[CorpusItem]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    items = data.get("item", [])
    if not isinstance(items, list) or not items:
        raise ValueError(f"{path}에 [[item]]이 없습니다. file = \"src/a.c\", function = \"f\" 형식으로 적으세요.")
    return [CorpusItem.model_validate(i) for i in items]


# ------------------------------------------------------------ 지표
def function_metrics(fs: FunctionState) -> FunctionMetrics:
    cats: dict[str, int] = {}
    built = 0
    for c in fs.cases:
        ran = any(h.status in RAN for h in c.history) or (c.last_failure is not None and c.last_failure.stage in ("run", "review", "shared", "final"))
        built += int(ran)
        if c.last_failure is not None:
            cats[c.last_failure.category] = cats.get(c.last_failure.category, 0) + 1
    count = lambda *st: sum(1 for c in fs.cases if c.status in st)  # noqa: E731
    return FunctionMetrics(
        function=fs.name, result=fs.result or "none", final_check=fs.final_check, written=len(fs.cases), built=built,
        passed=count(CaseStatus.PASS), dropped=count(CaseStatus.DROPPED), suspected_bugs=count(CaseStatus.SUSPECTED_BUG), flaky=count(CaseStatus.FLAKY),
        coverage=round(fs.coverage.percent, 1) if fs.coverage is not None and fs.coverage.total else None,
        fixes=sum(c.fixes_used for c in fs.cases), tokens=fs.tokens_used, failure_categories=cats,
    )


def summarize(items: list[ItemResult], metas: list[dict]) -> EvalSummary:
    fns = [f for it in items for f in it.functions]
    ok_items = [it for it in items if not it.error]
    n = max(1, len(fns))
    written = sum(f.written for f in fns)
    cats: dict[str, int] = {}
    for f in fns:
        for k, v in f.failure_categories.items():
            cats[k] = cats.get(k, 0) + v
    covs = [f.coverage for f in fns if f.coverage is not None]
    first = metas[0] if metas else {}
    return EvalSummary(
        provider=first.get("provider", ""), model=first.get("model", ""), items=len(items), functions=len(fns), errors=len(items) - len(ok_items),
        complete_rate=round(sum(f.result == "complete" for f in fns) / n, 3),
        salvaged_rate=round(sum(f.result in ("complete", "partial") for f in fns) / n, 3),
        none_rate=round(sum(f.result == "none" for f in fns) / n, 3),
        case_build_rate=round(sum(f.built for f in fns) / max(1, written), 3),
        case_pass_rate=round(sum(f.passed for f in fns) / max(1, written), 3),
        avg_coverage=round(sum(covs) / len(covs), 1) if covs else None,
        avg_builds=round(sum(it.builds for it in ok_items) / max(1, len(ok_items)), 2),
        avg_iterations=round(sum(it.iterations for it in ok_items) / max(1, len(ok_items)), 2),
        avg_seconds=round(sum(it.seconds for it in ok_items) / max(1, len(ok_items)), 1),
        avg_tokens=round(sum(f.tokens for f in fns) / n, 1),
        suspected_bugs=sum(f.suspected_bugs for f in fns),
        failure_categories=dict(sorted(cats.items(), key=lambda kv: -kv[1])),
        rules_hash=first.get("rules_hash", ""),
        prompts=first.get("prompts", {}),
    )


# ------------------------------------------------------------ 실행
def run_eval(
    ctx: AppContext,
    items: list[CorpusItem],
    *,
    corpus: str = "",
    label: str = "",
    provider: ChatProvider | None = None,
    backend_factory: Callable[[], Backend] | None = None,
    echo: Callable[[str], None] | None = None,
) -> tuple[EvalResult, Path]:
    root = ctx.config.product_root()
    started = datetime.now()
    if provider is None:  # 평가 하나에 provider 하나 (연결 재사용, 항목마다 같은 모델)
        from genut.llm.factory import make_provider

        provider = make_provider(ctx.config)
    results: list[ItemResult] = []
    metas: list[dict] = []
    for i, item in enumerate(items, 1):
        source = (root / item.file).resolve()
        if not source.is_file():
            results.append(ItemResult(file=item.file, function=item.function, error=f"파일이 없습니다: {source}"))
            continue
        opts = GenOptions(functions=[item.function] if item.function else [], install=False, notes=False)
        orch = Orchestrator(ctx, source, opts, provider=provider, backend=backend_factory() if backend_factory else None,
                            label=f"eval-{source.stem}" + (f"-{item.function}" if item.function else ""))
        st = orch.run()
        meta = json.loads((orch.rundir.path / "run.json").read_text(encoding="utf-8"))
        metas.append(meta)
        budget = meta.get("budget", {})
        it = ItemResult(file=item.file, function=item.function, run_id=st.run_id, error=orch.error, builds=budget.get("builds_used", 0),
                        iterations=budget.get("iterations", 0), seconds=budget.get("elapsed_sec", 0.0),
                        functions=[function_metrics(fs) for fs in st.functions.values()])
        results.append(it)
        if echo:
            echo(f"[{i}/{len(items)}] {item.file}" + (f":{item.function}" if item.function else "") + " → "
                 + (", ".join(f"{f.function} {f.result} ({f.passed}/{f.written})" for f in it.functions) or it.error or "-"))
    res = EvalResult(label=label, started=started.isoformat(timespec="seconds"), corpus=corpus, summary=summarize(results, metas), items=results)
    out = ctx.product.dir / "evals" / "runs" / (f"{started:%Y%m%d-%H%M%S}" + (f"-{_safe(label)}" if label else ""))
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(res.model_dump_json(indent=1), encoding="utf-8")
    return res, out


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "eval"


# ------------------------------------------------------------ 기준치
def baseline_path(home: Path, model: str) -> Path:
    return home / "evals" / "baselines" / f"{_safe(model)}.json"


def save_baseline(home: Path, result: EvalResult) -> Path:
    p = baseline_path(home, result.summary.model or "unknown")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(result.model_dump_json(indent=1), encoding="utf-8")
    return p


def load_result(path: Path) -> EvalResult:
    return EvalResult.model_validate_json(path.read_text(encoding="utf-8"))


METRICS = [
    ("complete_rate", "complete 비율", True, "%"), ("salvaged_rate", "일부라도 건진 비율", True, "%"), ("case_build_rate", "케이스 빌드율", True, "%"),
    ("case_pass_rate", "케이스 통과율", True, "%"), ("avg_coverage", "평균 라인 커버리지", True, "pct"), ("avg_builds", "평균 빌드 횟수", False, ""),
    ("avg_iterations", "평균 수정 라운드", False, ""), ("avg_tokens", "함수당 평균 토큰", False, ""), ("avg_seconds", "평균 소요(초)", False, ""),
]


def _fmt(v: object, unit: str) -> str:
    if v is None:
        return "-"
    if unit == "%":
        return f"{100 * float(v):.1f}%"
    if unit == "pct":
        return f"{float(v):.1f}%"
    return f"{float(v):g}"


def compare(cur: EvalSummary, base: EvalSummary | None) -> list[tuple[str, str, str, str]]:
    """(지표, 이번, 기준, 차이·방향) 목록."""
    rows = []
    for key, title, higher_better, unit in METRICS:
        a, b = getattr(cur, key), getattr(base, key) if base else None
        delta = ""
        if a is not None and b is not None and a != b:
            d = float(a) - float(b)
            good = (d > 0) == higher_better
            amount = f"{100 * d:+.1f}%p" if unit == "%" else f"{d:+.1f}{'%p' if unit == 'pct' else ''}"
            delta = f"{amount} {'개선' if good else '악화'}"
        rows.append((title, _fmt(a, unit), _fmt(b, unit), delta))
    return rows


# ------------------------------------------------------------ 리포트
def render_markdown(res: EvalResult, base: EvalResult | None = None) -> str:
    s = res.summary
    lines = [f"# GenUT 평가 {res.label or ''}".rstrip(), "", f"- 모델: {s.provider} · {s.model}", f"- 코퍼스: {res.corpus} ({s.items}개 항목, 함수 {s.functions}개, 오류 {s.errors})",
             f"- 규칙 md 해시: {s.rules_hash}", f"- 기준치: {base.summary.model + ' · ' + base.started if base else '없음'}", "",
             "| 지표 | 이번 | 기준 | 차이 |", "|---|---|---|---|"]
    lines += [f"| {t} | {a} | {b} | {d} |" for t, a, b, d in compare(s, base.summary if base else None)]
    lines += ["", "## 항목", "", "| 파일 | 함수 | 결과 | 통과/작성 | 커버리지 | 빌드 | 토큰 | 실행 |", "|---|---|---|---|---|---|---|---|"]
    for it in res.items:
        if it.error and not it.functions:
            lines.append(f"| {it.file} | {it.function or '-'} | 오류: {it.error} | | | | | {it.run_id} |")
        for f in it.functions:
            cov = f"{f.coverage:.0f}%" if f.coverage is not None else "-"
            lines.append(f"| {it.file} | {f.function} | {f.result} | {f.passed}/{f.written} | {cov} | {it.builds} | {f.tokens} | {it.run_id} |")
    if s.failure_categories:
        lines += ["", "## 실패 분류 분포", ""] + [f"- {k}: {v}" for k, v in s.failure_categories.items()]
    return "\n".join(lines) + "\n"


def render_html(res: EvalResult, base: EvalResult | None = None) -> str:
    import html

    from genut.viewer.render import CSS

    e = lambda x: html.escape(str(x))  # noqa: E731
    s = res.summary
    rows = "".join(
        f"<tr><td>{e(t)}</td><td><b>{e(a)}</b></td><td>{e(b)}</td><td class='{'ok' if '개선' in d else 'bad' if '악화' in d else 'muted'}'>{e(d)}</td></tr>"
        for t, a, b, d in compare(s, base.summary if base else None))
    items = ""
    for it in res.items:
        if it.error and not it.functions:
            items += f"<tr><td>{e(it.file)}</td><td>{e(it.function or '-')}</td><td class='bad' colspan='5'>오류: {e(it.error)}</td><td>{e(it.run_id)}</td></tr>"
        for f in it.functions:
            cls = {"complete": "ok", "partial": "warn", "none": "bad"}.get(f.result, "muted")
            cov = f"{f.coverage:.0f}%" if f.coverage is not None else "-"
            items += (f"<tr><td>{e(it.file)}</td><td>{e(f.function)}</td><td class='{cls}'>{e(f.result)}</td><td>{f.passed}/{f.written}</td>"
                      f"<td>{e(cov)}</td><td>{it.builds}</td><td>{f.tokens}</td><td>{e(it.run_id)}</td></tr>")
    cats = "".join(f"<tr><td>{e(k)}</td><td>{v}</td></tr>" for k, v in s.failure_categories.items())
    title = f"GenUT 평가 {res.label}".strip()
    body = (f"<h1>{e(title)}</h1><div class='sub'>{e(s.provider)} · {e(s.model)} · 코퍼스 {e(res.corpus)} · 항목 {s.items} · 함수 {s.functions} · 오류 {s.errors}"
            f" · 기준치 {e(base.summary.model + ' ' + base.started) if base else '없음'}</div>"
            f"<h2>지표</h2><table><tr><th>지표</th><th>이번</th><th>기준</th><th>차이</th></tr>{rows}</table>"
            f"<h2>항목</h2><table><tr><th>파일</th><th>함수</th><th>결과</th><th>통과/작성</th><th>커버리지</th><th>빌드</th><th>토큰</th><th>실행</th></tr>{items}</table>"
            + (f"<h2>실패 분류 분포</h2><table><tr><th>분류</th><th>케이스</th></tr>{cats}</table>" if cats else "")
            + f"<h2>버전</h2><pre>{e(json.dumps({'rules_hash': s.rules_hash, 'prompts': s.prompts}, ensure_ascii=False, indent=1))}</pre>")
    return f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>{e(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>\n"


def write_reports(res: EvalResult, out: Path, base: EvalResult | None = None) -> Path:
    (out / "report.md").write_text(render_markdown(res, base), encoding="utf-8")
    page = out / "report.html"
    page.write_text(render_html(res, base), encoding="utf-8")
    return page
