from pathlib import Path

import pytest
from typer.testing import CliRunner

from genut.cli import app
from genut.context import make_context
from genut.products import ProductError, add_product, detect_product, list_products, remove_product

runner = CliRunner()


def test_add_detect_remove(tmp_path):
    home = tmp_path / "h"
    src = tmp_path / "src" / "alpha"
    (src / "drivers").mkdir(parents=True)
    p = add_product(home, "alpha", src, "kunit")
    assert p.dir == home / "products" / "alpha"
    assert (p.dir / "runs").is_dir() and (p.dir / "knowledge").is_dir()
    assert (p.dir / "product.toml").exists()
    assert [x.name for x in list_products(home)] == ["alpha"]
    assert detect_product(home, src / "drivers").name == "alpha"
    assert detect_product(home, tmp_path) is None
    # 소스 트리 안에는 아무것도 쓰지 않는다
    assert list(src.rglob("*.toml")) == []
    remove_product(home, "alpha", delete_data=True)
    assert list_products(home) == []
    assert not p.dir.exists()


def test_overlapping_roots_rejected(tmp_path):
    home = tmp_path / "h"
    (tmp_path / "a" / "b").mkdir(parents=True)
    add_product(home, "a", tmp_path / "a")
    with pytest.raises(ProductError):
        add_product(home, "b", tmp_path / "a" / "b")
    with pytest.raises(ProductError):
        add_product(home, "a", tmp_path / "a")


def test_bad_name_rejected(tmp_path):
    with pytest.raises(ProductError):
        add_product(tmp_path / "h", "a/b", tmp_path)


def test_context_uses_product_config(tmp_path):
    home = tmp_path / "h"
    src = tmp_path / "s"
    src.mkdir()
    p = add_product(home, "k", src, "kunit")
    ctx = make_context(home, cwd=src, overrides=["budget.candidates=4"])
    assert ctx.product.name == "k"
    assert ctx.config.backend == "kunit"
    assert ctx.config.budget.candidates == 4
    assert ctx.config.budget.max_builds_per_run == 10
    assert ctx.runs_root == p.dir / "runs"


def test_cli_init_and_product(tmp_path):
    home = tmp_path / "h"
    r = runner.invoke(app, ["init", "--home", str(home)])
    assert r.exit_code == 0, r.output
    assert (home / "config.toml").exists()
    assert (home / "rules" / "kunit.md").exists()
    src = tmp_path / "s"
    src.mkdir()
    r = runner.invoke(app, ["product", "add", "beta", "--root", str(src), "--home", str(home)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["product", "list", "--home", str(home)])
    assert "beta" in r.output
    r = runner.invoke(app, ["product", "add", "beta", "--root", str(src), "--home", str(home)])
    assert r.exit_code == 2
    r = runner.invoke(app, ["product", "remove", "beta", "--home", str(home)])
    assert r.exit_code == 0


def test_cli_doctor_runs(tmp_path):
    home = tmp_path / "h"
    runner.invoke(app, ["init", "--home", str(home)])
    r = runner.invoke(app, ["doctor", "--home", str(home)])
    assert "GENUT_HOME" in r.output
    assert "libclang" in r.output


def test_show_without_runs(tmp_path):
    home = tmp_path / "h"
    src = tmp_path / "s"
    src.mkdir()
    add_product(home, "g", src)
    r = runner.invoke(app, ["show", "latest", "--home", str(home), "--product", "g"])
    assert r.exit_code == 2
    assert "실행 기록" in r.output


def test_doctor_checks_coverage_tools(tmp_path, monkeypatch):
    from genut import doctor
    from genut.doctor import collect_checks

    src = tmp_path / "s"
    src.mkdir()
    add_product(tmp_path / "h", "g", src)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    ctx = make_context(tmp_path / "h", "g", overrides=["coverage.tool=llvm-cov", "coverage.enabled=true"])
    cov = next(c for c in collect_checks(ctx) if c.title == "커버리지 도구")
    assert cov.status == "warn" and "llvm-profdata, llvm-cov" in cov.detail
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    cov = next(c for c in collect_checks(make_context(tmp_path / "h", "g", overrides=["coverage.enabled=true"])) if c.title == "커버리지 도구")
    assert cov.status == "ok" and cov.detail == "/usr/bin/gcov"
    off = collect_checks(make_context(tmp_path / "h", "g"))  # 기본은 꺼짐: 도구를 찾지 않고 켜는 방법을 알려 준다
    assert not [c for c in off if c.title == "커버리지 도구"]
    assert next(c for c in off if c.title == "커버리지").detail.startswith("꺼짐 (선택 기능). 켜려면 genut gen --coverage")


def test_coverage_cli_option():
    from genut.cli import _with_coverage

    assert _with_coverage(["budget.candidates=3"], True) == ["budget.candidates=3", "coverage.enabled=true"]
    assert _with_coverage(None, False) == ["coverage.enabled=false"]
    assert _with_coverage(["x.y=1"], None) == ["x.y=1"]  # 옵션을 안 주면 설정 파일을 따른다
