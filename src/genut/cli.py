"""GenUT 명령줄 (설계 11.1)."""

from __future__ import annotations

import sys
from importlib import resources
from pathlib import Path
from typing import Annotated, Optional

import typer

from genut import __version__
from genut.context import make_context
from genut.paths import ensure_home_layout, genut_home
from genut.products import ProductError, add_product, list_products, remove_product

app = typer.Typer(add_completion=False, no_args_is_help=True, help="gtest·KUnit 단위 테스트 자동 생성기")
product_app = typer.Typer(no_args_is_help=True, help="프로덕트 등록 관리")
knowledge_app = typer.Typer(no_args_is_help=True, help="축적 지식 관리")
app.add_typer(product_app, name="product")
app.add_typer(knowledge_app, name="knowledge")

HomeOpt = Annotated[Optional[Path], typer.Option("--home", help="GENUT_HOME 대신 쓸 경로")]
ProductOpt = Annotated[Optional[str], typer.Option("--product", "-p", help="프로덕트 이름 (생략하면 현재 디렉터리로 탐지)")]
SetOpt = Annotated[Optional[list[str]], typer.Option("--set", help="설정 덮어쓰기. 예: --set budget.candidates=3")]


def _fail(msg: str, code: int = 2) -> None:
    typer.secho(f"오류: {msg}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


def _home(home: Path | None) -> Path:
    return (home or genut_home()).expanduser()


@app.command()
def version() -> None:
    """버전을 보여 준다."""
    typer.echo(f"genut {__version__}")


# ------------------------------------------------------------------- init
@app.command()
def init(home: HomeOpt = None, force: Annotated[bool, typer.Option(help="있는 템플릿도 덮어쓴다")] = False) -> None:
    """GENUT_HOME에 공통 설정과 규칙 템플릿을 만든다."""
    h = _home(home)
    ensure_home_layout(h)
    tpl = resources.files("genut") / "templates"
    written = []
    targets = [("config.toml", h / "config.toml")] + [
        (f"rules/{n}", h / "rules" / n) for n in ("common.md", "gtest.md", "kunit.md")
    ]
    for src, dst in targets:
        if dst.exists() and not force:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text((tpl / src).read_text(encoding="utf-8"), encoding="utf-8")
        written.append(dst)
    typer.echo(f"GENUT_HOME: {h}")
    for p in written:
        typer.echo(f"  만듦: {p}")
    if not written:
        typer.echo("  이미 준비되어 있습니다 (--force로 템플릿을 다시 쓸 수 있음).")
    typer.echo("다음: genut product add <이름> --root <소스 트리> --backend gtest|kunit")


# ---------------------------------------------------------------- product
@product_app.command("add")
def product_add(
    name: str,
    root: Annotated[Path, typer.Option("--root", help="프로덕트 소스 트리 루트")],
    backend: Annotated[str, typer.Option(help="gtest | kunit")] = "gtest",
    home: HomeOpt = None,
) -> None:
    """프로덕트를 등록하고 $GENUT_HOME/products/<이름>/을 만든다."""
    if backend not in ("gtest", "kunit"):
        _fail("backend는 gtest 또는 kunit 이어야 합니다.")
    h = _home(home)
    ensure_home_layout(h)
    try:
        p = add_product(h, name, root, backend)
    except ProductError as e:
        _fail(str(e))
    typer.echo(f"등록: {p.name}  소스 트리 {p.root}")
    typer.echo(f"프로덕트 폴더: {p.dir}")
    typer.echo(f"설정 파일을 채우세요: {p.product_toml}")


@product_app.command("list")
def product_list(home: HomeOpt = None) -> None:
    """등록된 프로덕트 목록."""
    items = list_products(_home(home))
    if not items:
        typer.echo("등록된 프로덕트가 없습니다.")
    for p in items:
        typer.echo(f"{p.name}\t{p.root}")


@product_app.command("remove")
def product_remove(
    name: str,
    delete_data: Annotated[bool, typer.Option("--delete-data", help="프로덕트 폴더(실행 기록, 지식)도 지운다")] = False,
    home: HomeOpt = None,
) -> None:
    """프로덕트 등록을 해제한다."""
    try:
        remove_product(_home(home), name, delete_data)
    except ProductError as e:
        _fail(str(e))
    typer.echo(f"해제: {name}" + (" (프로덕트 폴더 삭제)" if delete_data else ""))


# ----------------------------------------------------------------- doctor
@app.command()
def doctor(home: HomeOpt = None, product: ProductOpt = None, set_: SetOpt = None) -> None:
    """환경을 점검하고 문제와 해결 방법을 한국어로 알려 준다."""
    from genut.doctor import run_doctor

    try:
        ctx = make_context(_home(home), product, overrides=set_)
    except (ProductError, ValueError) as e:
        _fail(str(e))
    ok = run_doctor(ctx, typer.echo)
    raise typer.Exit(0 if ok else 1)


# ------------------------------------------------------------------- show
@app.command()
def show(run: Annotated[str, typer.Argument(help="실행 ID 또는 latest")] = "latest", home: HomeOpt = None, product: ProductOpt = None) -> None:
    """실행 하나의 요약을 터미널에 보여 준다."""
    from genut.report import render_summary
    from genut.trace import resolve_run

    try:
        ctx = make_context(_home(home), product, require_product=True)
        path = resolve_run(ctx.runs_root, run)
    except (ProductError, FileNotFoundError) as e:
        _fail(str(e))
    typer.echo(render_summary(path))


# -------------------------------------------------------------------- gen
@app.command()
def gen(
    file: Annotated[Path, typer.Option("--file", "-f", help="대상 소스 파일")],
    func: Annotated[Optional[list[str]], typer.Option("--func", help="대상 함수 (여러 번 줄 수 있음. 생략하면 모든 비static 함수)")] = None,
    backend: Annotated[Optional[str], typer.Option("--backend", help="gtest | kunit (생략하면 프로덕트 설정)")] = None,
    out: Annotated[Optional[Path], typer.Option("--out", help="함수별 테스트 파일을 쓸 위치 (gtest output_dir 덮어쓰기)")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="LLM·빌드 없이 각 단계 프롬프트만 만들어 기록")] = False,
    stop_after: Annotated[Optional[str], typer.Option("--stop-after", help="이 단계 뒤에 멈춤: analyze|stub|preflight|smoke|expand|fix")] = None,
    home: HomeOpt = None,
    product: ProductOpt = None,
    set_: SetOpt = None,
) -> None:
    """테스트를 생성한다. 테스트 파일은 함수별로, 빌드는 소스 파일 단위로 묶는다."""
    from genut.orchestrator import STAGES, GenOptions, Orchestrator

    if stop_after and stop_after not in STAGES:
        _fail(f"--stop-after는 {', '.join(STAGES)} 중 하나여야 합니다.")
    source = file.expanduser().resolve()
    if not source.is_file():
        _fail(f"파일이 없습니다: {source}")
    overrides = list(set_ or [])
    if backend:
        overrides.append(f"product.backend={backend}")
    if out:
        overrides.append(f"gtest.output_dir={out.expanduser().resolve()}")
    try:
        ctx = make_context(_home(home), product, overrides=overrides, require_product=True)
    except (ProductError, ValueError) as e:
        _fail(str(e))
    orch = Orchestrator(ctx, source, GenOptions(functions=list(func or []), dry_run=dry_run, stop_after=stop_after or ""), echo=typer.echo)
    orch.run()
    if orch.error:
        raise typer.Exit(1)


# --------------------------------------------------------------- finalize
@app.command()
def finalize(run: Annotated[str, typer.Argument(help="실행 ID 또는 latest")] = "latest", home: HomeOpt = None, product: ProductOpt = None) -> None:
    """중단되었거나 미확인으로 남은 실행의 통과 케이스로 최종 확인만 다시 해서 설치한다 (LLM 호출 없음)."""
    from genut.orchestrator import StopRun, finalize_run
    from genut.trace import resolve_run

    try:
        ctx = make_context(_home(home), product, require_product=True)
        finalize_run(ctx, resolve_run(ctx.runs_root, run), echo=typer.echo)
    except (ProductError, FileNotFoundError, StopRun) as e:
        _fail(str(e), 1)


# ------------------------------------------------------------------- view
@app.command()
def view(
    run: Annotated[str, typer.Argument(help="실행 ID 또는 latest")] = "latest",
    open_: Annotated[bool, typer.Option("--open/--no-open", help="브라우저로 연다")] = True,
    home: HomeOpt = None,
    product: ProductOpt = None,
) -> None:
    """실행 하나를 단일 HTML 파일로 만든다."""
    from genut.trace import resolve_run
    from genut.viewer.render import write_view

    try:
        ctx = make_context(_home(home), product, require_product=True)
        path = write_view(resolve_run(ctx.runs_root, run))
    except (ProductError, FileNotFoundError) as e:
        _fail(str(e))
    typer.echo(f"뷰어: {path}")
    if open_:
        import webbrowser

        webbrowser.open(path.as_uri())


# ----------------------------------------------------------------- replay
@app.command()
def replay(
    run: Annotated[str, typer.Argument(help="실행 ID 또는 latest")],
    from_: Annotated[Optional[str], typer.Option("--from", help="이 단계부터는 실제 LLM을 부른다 (생략하면 기록된 응답만)")] = None,
    home: HomeOpt = None,
    product: ProductOpt = None,
    set_: SetOpt = None,
) -> None:
    """기록된 LLM 응답을 재사용해 다시 실행한다. 분류 규칙·축약·예산을 바꾼 효과를 비용 없이 본다."""
    from genut.replay import replay_run
    from genut.trace import resolve_run

    try:
        ctx = make_context(_home(home), product, overrides=set_, require_product=True)
        orch = replay_run(ctx, resolve_run(ctx.runs_root, run), from_ or "", echo=typer.echo)
    except (ProductError, FileNotFoundError, ValueError) as e:
        _fail(str(e))
    if orch.error:
        raise typer.Exit(1)


# ------------------------------------------------------------------- eval
@app.command("eval")
def eval_(
    corpus: Annotated[Optional[Path], typer.Option("--corpus", help="코퍼스 파일 (생략하면 products/<이름>/evals/corpus/corpus.toml)")] = None,
    label: Annotated[str, typer.Option("--label", help="이번 평가의 이름 (예: 규칙 v3)")] = "",
    save_baseline: Annotated[bool, typer.Option("--save-baseline", help="이번 결과를 이 모델의 기준치로 저장")] = False,
    compare_to: Annotated[Optional[Path], typer.Option("--compare", help="비교할 results.json (생략하면 이 모델의 기준치)")] = None,
    open_: Annotated[bool, typer.Option("--open/--no-open", help="HTML 리포트를 브라우저로 연다")] = False,
    home: HomeOpt = None,
    product: ProductOpt = None,
    set_: SetOpt = None,
) -> None:
    """코퍼스 전체의 성공률을 재고 기준치와 비교한다. 프로덕트 트리에는 설치하지 않는다."""
    from genut.evaluation import baseline_path, compare, corpus_path, load_corpus, load_result, run_eval, write_reports
    from genut.evaluation import save_baseline as store_baseline

    try:
        ctx = make_context(_home(home), product, overrides=set_, require_product=True)
    except (ProductError, ValueError) as e:
        _fail(str(e))
    path = (corpus or corpus_path(ctx)).expanduser()
    if not path.is_file():
        _fail(f"코퍼스가 없습니다: {path}\n[[item]] 마다 file = \"src/a.c\", function = \"f\" 를 적은 TOML을 만드세요.")
    try:
        items = load_corpus(path)
    except (ValueError, OSError) as e:
        _fail(str(e))
    res, out = run_eval(ctx, items, corpus=str(path), label=label, echo=typer.echo)
    base_file = compare_to or baseline_path(ctx.home, res.summary.model)
    base = load_result(base_file) if base_file.is_file() else None
    page = write_reports(res, out, base)
    for title, cur, ref, delta in compare(res.summary, base.summary if base else None):
        typer.echo(f"  {title:<14} {cur:>8}" + (f"  (기준 {ref}, {delta})" if base and delta else ""))
    typer.echo(f"리포트: {page}")
    if save_baseline:
        typer.echo(f"기준치 저장: {store_baseline(ctx.home, res)}")
    if open_:
        import webbrowser

        webbrowser.open(page.as_uri())


def main() -> None:
    # Windows 콘솔에서 한국어가 깨지지 않게
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    app()

