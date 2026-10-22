"""프로덕트 등록과 자동 탐지 (설계 11.5)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from genut.paths import ensure_product_layout, is_relative_to, product_dir
from genut.tomlio import load_toml, write_toml


class ProductError(Exception):
    pass


@dataclass(frozen=True)
class Product:
    name: str
    root: Path
    dir: Path

    @property
    def product_toml(self) -> Path:
        return self.dir / "product.toml"


def _registry_path(home: Path) -> Path:
    return home / "products.toml"


def list_products(home: Path) -> list[Product]:
    reg = load_toml(_registry_path(home)).get("products", {})
    return [
        Product(name=name, root=Path(entry.get("root", "")), dir=product_dir(name, home))
        for name, entry in sorted(reg.items())
    ]


def get_product(home: Path, name: str) -> Product:
    for p in list_products(home):
        if p.name == name:
            return p
    raise ProductError(f"등록되지 않은 프로덕트입니다: {name}. `genut product list`로 확인하세요.")


def add_product(home: Path, name: str, root: Path, backend: str = "gtest") -> Product:
    if not name or any(c in name for c in "/\\:. ") or name.startswith("-"):
        raise ProductError(f"프로덕트 이름에 쓸 수 없는 문자가 있습니다: {name!r}")
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ProductError(f"소스 트리 경로가 디렉터리가 아닙니다: {root}")
    reg = load_toml(_registry_path(home))
    products = reg.setdefault("products", {})
    if name in products:
        raise ProductError(f"이미 등록된 프로덕트입니다: {name}")
    for other, entry in products.items():
        oroot = Path(entry.get("root", ""))
        if oroot and (is_relative_to(root, oroot) or is_relative_to(oroot, root)):
            raise ProductError(f"소스 트리가 프로덕트 {other}({oroot})와 겹칩니다.")
    products[name] = {"root": str(root)}
    write_toml(_registry_path(home), reg)

    pdir = product_dir(name, home)
    ensure_product_layout(pdir)
    if not (pdir / "product.toml").exists():
        write_toml(pdir / "product.toml", {"product": {"name": name, "root": str(root), "backend": backend}})
    return Product(name=name, root=root, dir=pdir)


def remove_product(home: Path, name: str, delete_data: bool = False) -> None:
    reg = load_toml(_registry_path(home))
    products = reg.get("products", {})
    if name not in products:
        raise ProductError(f"등록되지 않은 프로덕트입니다: {name}")
    del products[name]
    write_toml(_registry_path(home), reg)
    if delete_data:
        shutil.rmtree(product_dir(name, home), ignore_errors=True)


def detect_product(home: Path, cwd: Path) -> Product | None:
    """현재 디렉터리가 속한 등록 소스 트리를 찾는다. 겹치면 가장 깊은 것."""
    best: Product | None = None
    for p in list_products(home):
        if p.root and p.root.exists() and is_relative_to(cwd, p.root):
            if best is None or len(str(p.root)) > len(str(best.root)):
                best = p
    return best
