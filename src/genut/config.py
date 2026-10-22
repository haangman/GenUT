"""설정 모델과 겹쳐 올리기 (설계 11.5, 12장).

우선순위: 내장 기본값 < 백엔드 기본값 < $GENUT_HOME/config.toml < product.toml < CLI 덮어쓰기
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from genut.tomlio import load_toml


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------- LLM
class AnthropicConfig(_M):
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: str = "https://api.anthropic.com"
    max_context: int = 200_000
    timeout_sec: float = 300.0


class OpenAICompatConfig(_M):
    base_url: str = "http://gemma4.internal/v1"
    model: str = "gemma-4-12b"
    api_key_env: str = "GENUT_LLM_KEY"
    max_context: int = 32_768
    supports_json_schema: bool = True
    supports_n: bool = False
    timeout_sec: float = 300.0


class FakeLLMConfig(_M):
    script: str = ""  # FakeProvider가 읽을 응답 스크립트 (JSON). 테스트·dry-run용
    max_context: int = 32_768


class ContextConfig(_M):
    output_reserve_tokens: int = 6000
    context_safety_margin: float = 0.05
    token_counter: Literal["estimate", "api"] = "estimate"


class LLMConfig(_M):
    provider: Literal["anthropic", "openai_compat", "fake"] = "anthropic"
    model: str = "claude-sonnet-5-5"
    temperature: float = 0.2
    prompt_language: Literal["en", "ko"] = "en"
    code_format: Literal["json", "fenced"] = "json"
    max_retries: int = 4
    anthropic: AnthropicConfig = Field(default_factory=AnthropicConfig)
    openai_compat: OpenAICompatConfig = Field(default_factory=OpenAICompatConfig)
    fake: FakeLLMConfig = Field(default_factory=FakeLLMConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)


# ------------------------------------------------------------------ budget
class BudgetConfig(_M):
    candidates: int = 1
    parallel_candidates: bool = False
    max_iterations: int = 4
    max_case_fixes: int = 2
    max_isolation_runs: int = 4
    max_builds_per_run: int = 30
    max_seconds_per_run: int = 1800
    max_tokens_per_function: int = 300_000
    max_coverage_rounds: int = 3
    final_build_reserve: int = 2
    slow_build_threshold_sec: int = 120
    log_token_budget: int = 4000
    analysis_token_budget: int = 3000
    knowledge_token_budget: int = 1500
    passed_cases_token_budget: int = 4000
    fix_examples_token_budget: int = 1500


class CoverageConfig(_M):
    enabled: bool = True
    target_line_percent: float = 100.0
    tool: Literal["gcov", "llvm-cov"] = "gcov"


class FinalizeConfig(_M):
    install_unverified: bool = False


class KnowledgeConfig(_M):
    auto_promote_after_products: int = 2


class AnalysisConfig(_M):
    include_struct_fields: bool = True
    max_struct_fields: int = 30
    libclang_path: str = ""  # 비우면 libclang 패키지가 제공하는 라이브러리 사용


# ---------------------------------------------------------------- backends
class GTestConfig(_M):
    cmake_exe: str = "cmake"
    cmake_source_dir: str = "."
    cmake_build_dir: str = "build-genut"
    cmake_configure_args: list[str] = Field(default_factory=list)
    cmake_lists_dir: str = "tests/generated"
    output_dir: str = "tests/generated"
    test_file_pattern: str = "{src}_{func}_test.cc"
    link_targets: list[str] = Field(default_factory=list)
    gtest_root: str = ""
    sanitizers: list[str] = Field(default_factory=list)
    coverage_opt_level: str = "O0"
    crash_backtrace: str = "gdb"
    test_timeout_sec: int = 60


class KUnitConfig(_M):
    compile_commands: str = ""
    kernel_src: str = ""
    test_file: str = ""
    test_dir: str = ""
    test_file_pattern: str = "{src}_{func}_test.c"
    run_script: str = ""
    script_supports_filter: bool = False
    script_timeout_sec: int = 3600
    vmlinux: str = ""
    static_access: Literal["stub_tool", "include_source"] = "stub_tool"


class ProductSection(_M):
    name: str = ""
    root: str = ""
    backend: Literal["gtest", "kunit"] = "gtest"


class GenutConfig(_M):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    coverage: CoverageConfig = Field(default_factory=CoverageConfig)
    finalize: FinalizeConfig = Field(default_factory=FinalizeConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    product: ProductSection = Field(default_factory=ProductSection)
    gtest: GTestConfig = Field(default_factory=GTestConfig)
    kunit: KUnitConfig = Field(default_factory=KUnitConfig)

    @property
    def backend(self) -> str:
        return self.product.backend

    def product_root(self) -> Path:
        return Path(self.product.root).expanduser() if self.product.root else Path.cwd()


# KUnit은 빌드가 느려(QEMU 약 8분) 예산 기본값을 다르게 둔다 (설계 7.7, 7.10).
BACKEND_DEFAULTS: dict[str, dict[str, Any]] = {
    "gtest": {},
    "kunit": {
        "budget": {
            "max_builds_per_run": 10,
            "max_seconds_per_run": 7200,
            "max_isolation_runs": 2,
            "final_build_reserve": 1,
        }
    },
}


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def parse_override(text: str) -> dict[str, Any]:
    """'budget.candidates=3' 형태의 CLI 덮어쓰기를 중첩 dict로 바꾼다."""
    if "=" not in text:
        raise ValueError(f"덮어쓰기 형식은 key=value 입니다: {text!r}")
    key, raw = text.split("=", 1)
    value: Any
    low = raw.strip().lower()
    if low in ("true", "false"):
        value = low == "true"
    else:
        try:
            value = int(raw)
        except ValueError:
            try:
                value = float(raw)
            except ValueError:
                value = [x.strip() for x in raw[1:-1].split(",") if x.strip()] if raw.startswith("[") else raw
    node: dict[str, Any] = {}
    cur = node
    parts = key.strip().split(".")
    for p in parts[:-1]:
        cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value
    return node


class LoadedConfig(BaseModel):
    """겹쳐 올린 최종 설정과 그 출처(doctor가 보여 줌)."""

    config: GenutConfig
    sources: list[str]


def load_config(
    home: Path,
    product_toml: Path | None = None,
    overrides: list[dict[str, Any]] | None = None,
) -> LoadedConfig:
    sources = ["내장 기본값"]
    common = load_toml(home / "config.toml")
    if common:
        sources.append(str(home / "config.toml"))
    product = load_toml(product_toml) if product_toml else {}
    if product:
        sources.append(str(product_toml))

    backend = (
        (overrides_backend(overrides) or product.get("product", {}).get("backend"))
        or common.get("product", {}).get("backend")
        or "gtest"
    )
    data: dict[str, Any] = deep_merge({}, BACKEND_DEFAULTS.get(backend, {}))
    if BACKEND_DEFAULTS.get(backend):
        sources.append(f"{backend} 백엔드 기본값")
    data = deep_merge(data, common)
    data = deep_merge(data, product)
    for ov in overrides or []:
        data = deep_merge(data, ov)
    if overrides:
        sources.append("CLI 덮어쓰기")
    return LoadedConfig(config=GenutConfig.model_validate(data), sources=sources)


def overrides_backend(overrides: list[dict[str, Any]] | None) -> str | None:
    for ov in overrides or []:
        b = ov.get("product", {}).get("backend")
        if b:
            return b
    return None
