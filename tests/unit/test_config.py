import pytest
from pydantic import ValidationError

from genut.config import GenutConfig, deep_merge, load_config, parse_override
from genut.tomlio import dumps_toml, load_toml, write_toml


def test_defaults_match_design():
    cfg = GenutConfig()
    assert cfg.budget.max_tokens_per_function == 300_000
    assert cfg.budget.final_build_reserve == 2
    assert cfg.llm.provider == "anthropic"
    assert cfg.llm.openai_compat.supports_n is False
    assert cfg.kunit.static_access == "stub_tool"


def test_layering_order(tmp_path):
    home = tmp_path / "h"
    write_toml(home / "config.toml", {"budget": {"candidates": 2, "max_iterations": 5}, "llm": {"temperature": 0.5}})
    ptoml = tmp_path / "p" / "product.toml"
    write_toml(ptoml, {"product": {"name": "a", "backend": "gtest"}, "budget": {"candidates": 3}})
    loaded = load_config(home, ptoml, [parse_override("budget.max_iterations=7")])
    c = loaded.config
    assert c.budget.candidates == 3  # 프로덕트가 공통을 이김
    assert c.budget.max_iterations == 7  # CLI가 모두를 이김
    assert c.llm.temperature == 0.5  # 공통 값 유지
    assert loaded.sources[0] == "내장 기본값"
    assert loaded.sources[-1] == "CLI 덮어쓰기"


def test_kunit_backend_defaults_apply_under_user_files(tmp_path):
    home = tmp_path / "h"
    ptoml = tmp_path / "product.toml"
    write_toml(ptoml, {"product": {"backend": "kunit"}})
    c = load_config(home, ptoml).config
    assert c.budget.max_builds_per_run == 10
    assert c.budget.final_build_reserve == 1
    write_toml(ptoml, {"product": {"backend": "kunit"}, "budget": {"max_builds_per_run": 12}})
    assert load_config(home, ptoml).config.budget.max_builds_per_run == 12


def test_unknown_key_rejected(tmp_path):
    home = tmp_path / "h"
    write_toml(home / "config.toml", {"budget": {"no_such_key": 1}})
    with pytest.raises(ValidationError):
        load_config(home)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("budget.candidates=3", {"budget": {"candidates": 3}}),
        ("llm.temperature=0.7", {"llm": {"temperature": 0.7}}),
        ("coverage.enabled=false", {"coverage": {"enabled": False}}),
        ("llm.provider=fake", {"llm": {"provider": "fake"}}),
        ("gtest.link_targets=[a, b]", {"gtest": {"link_targets": ["a", "b"]}}),
    ],
)
def test_parse_override(text, expected):
    assert parse_override(text) == expected


def test_parse_override_requires_equals():
    with pytest.raises(ValueError):
        parse_override("budget.candidates")


def test_deep_merge_does_not_mutate():
    a = {"x": {"y": 1}}
    b = {"x": {"z": 2}}
    m = deep_merge(a, b)
    assert m == {"x": {"y": 1, "z": 2}}
    assert a == {"x": {"y": 1}}


def test_toml_roundtrip(tmp_path):
    data = {"product": {"name": "foo", "root": 'C:\\src\\"x"', "backend": "kunit"}, "budget": {"candidates": 3, "flag": True, "lst": ["a", "b"]}}
    p = tmp_path / "x.toml"
    write_toml(p, data)
    assert load_toml(p) == data
    assert "[product]" in dumps_toml(data)
