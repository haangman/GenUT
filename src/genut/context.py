"""CLI 명령이 공통으로 쓰는 실행 문맥: 홈, 프로덕트, 겹쳐 올린 설정."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from genut.config import GenutConfig, load_config, parse_override
from genut.paths import genut_home
from genut.products import Product, ProductError, detect_product, get_product


@dataclass
class AppContext:
    home: Path
    product: Product | None
    config: GenutConfig
    sources: list[str]

    @property
    def runs_root(self) -> Path:
        if self.product is None:
            raise ProductError("프로덕트를 찾지 못했습니다. 등록된 소스 트리 안에서 실행하거나 --product를 주세요.")
        return self.product.dir / "runs"


def make_context(
    home: Path | None = None,
    product_name: str | None = None,
    cwd: Path | None = None,
    overrides: list[str] | None = None,
    extra: list[dict[str, Any]] | None = None,
    require_product: bool = False,
) -> AppContext:
    home = home or genut_home()
    product: Product | None
    if product_name:
        product = get_product(home, product_name)
    else:
        product = detect_product(home, cwd or Path.cwd())
    if require_product and product is None:
        raise ProductError(
            "현재 디렉터리가 등록된 프로덕트의 소스 트리에 속하지 않습니다. "
            "`genut product add <이름> --root <경로>`로 등록하거나 --product를 주세요."
        )
    ovs = [parse_override(o) for o in overrides or []] + list(extra or [])
    loaded = load_config(home, product.product_toml if product else None, ovs)
    cfg = loaded.config
    if product and not cfg.product.root:
        cfg.product.root = str(product.root)
    if product and not cfg.product.name:
        cfg.product.name = product.name
    return AppContext(home=home, product=product, config=cfg, sources=loaded.sources)
