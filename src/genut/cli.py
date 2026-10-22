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


def main() -> None:
    # Windows 콘솔에서 한국어가 깨지지 않게
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    app()

