"""genut diff (설계 11.1, 11.4): 바뀐 입력과 바뀐 결과를 실행 기록만으로 비교한다."""

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from conftest import SAMPLE, write_compile_db
from fake_backend import MarkerBackend
from genut.cli import app
from genut.context import make_context
from genut.llm.fake_provider import FakeProvider
from genut.orchestrator import GenOptions, Orchestrator
from genut.paths import ensure_home_layout
from genut.products import add_product
from genut.replay import replay_run
from genut.rundiff import config_changes, diff_runs, flatten, load_run, render_diff
from genut.trace import RunDir, find_runs, resolve_run

RB = SAMPLE / "src" / "ring_buffer.c"
OK = 'struct rb rb;\nunsigned char buf[8];\nrb_init(&rb, buf, 8);\nEXPECT_EQ(rb_push(&rb, "a", 1), RB_OK);'
FULL_BAD = 'EXPECT_EQ(rb_push(&rb, "abcdef", 6), RB_OK); // FAIL ACTUAL=-28'
FULL_OK = 'EXPECT_EQ(rb_push(&rb, "abcdef", 6), RB_ENOSPC);'
ANALYZED = {"testable": True, "reason": "", "cautions": [], "recommended": []}


def _cases(*items):
    return {"cases": [{"name": n, "purpose": n, "body": b} for n, b in items], "helpers": []}


def _fix(strategy, body=""):
    return {"strategy": strategy, "reason": "이유", "body": body, "evidence": ":34 RB_ENOSPC", "suspected_bug": False, "helpers": []}


WRITER = [_cases(("test_rb_push_ok", OK)), _cases(("test_rb_push_full", FULL_BAD))]


def test_flatten_and_config_changes():
    assert flatten({"a": {"b": 1, "c": {"d": [1, 2]}}, "e": {}}) == {"a.b": 1, "a.c.d": [1, 2], "e": {}}
    changes = config_changes({"budget": {"max_case_fixes": 2, "candidates": 1}, "gtest": {"link_targets": []}},
                             {"budget": {"max_case_fixes": 3, "candidates": 1}, "gtest": {"link_targets": ["rb"]}, "new": {"x": 1}})
    assert changes == [{"key": "budget.max_case_fixes", "a": 2, "b": 3}, {"key": "gtest.link_targets", "a": [], "b": ["rb"]},
                       {"key": "new.x", "a": None, "b": 1}]


def test_resolve_run_latest_n(tmp_path):
    from datetime import datetime

    root = tmp_path / "runs"
    made = [RunDir.create(root, label, datetime(2026, 10, 22, 14, 30, 12)).path for label in ("zeta", "alpha", "mid")]
    assert find_runs(root) == made  # 같은 초에 만들면 라벨 알파벳순이 아니라 만든 순서
    assert resolve_run(root, "latest") == made[2] and resolve_run(root, "latest~2") == made[0]
    with pytest.raises(FileNotFoundError, match="3개뿐"):
        resolve_run(root, "latest~3")


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    ensure_home_layout(home)
    add_product(home, "sample", SAMPLE, "gtest")
    be = MarkerBackend(write_compile_db(tmp_path / "cc.json"), tmp_path / "installed", [RB, SAMPLE / "src" / "slot.c"])
    return SimpleNamespace(home=home, be=be)


def _gen(world, fixer, overrides=()):
    p = FakeProvider(script={"analyzer": [ANALYZED], "test_writer": WRITER, "fixer": [fixer]})
    orch = Orchestrator(make_context(world.home, "sample", overrides=list(overrides)), RB, GenOptions(functions=["rb_push"]), provider=p, backend=world.be)
    orch.run()
    return orch.rundir.path


@pytest.mark.libclang
def test_diff_shows_changed_inputs_and_results(world):
    a = _gen(world, _fix("drop_case"))
    b = _gen(world, _fix("fix_case", FULL_OK), overrides=["budget.max_case_fixes=3"])
    d = diff_runs(load_run(a), load_run(b))
    assert d["inputs"]["config"] == [{"key": "budget.max_case_fixes", "a": 2, "b": 3}]
    assert not d["inputs"]["rules_changed"] and d["inputs"]["prompts_changed"] == [] and d["inputs"]["source_changed"] == []
    ma, mb = d["metrics"]["a"], d["metrics"]["b"]
    assert (ma["results"], mb["results"]) == ({"partial": 1}, {"complete": 1})
    assert ma["statuses"] == {"pass": 1, "dropped": 1} and mb["statuses"] == {"pass": 2}
    assert ma["fixes"] == mb["fixes"] == {"assertion_failed": 1} and mb["llm_calls"]["fixer"] == 1
    [f] = d["functions"]
    [c] = f["cases"]
    assert f["unchanged"] == 1 and c["name"] == "test_rb_push_full" and c["body_changed"]
    assert (c["a"]["path"], c["b"]["path"]) == ("new → fail → dropped", "new → fail → new → pass")

    text = render_diff(d)
    assert "설정 budget.max_case_fixes: 2 → 3" in text and "함수 complete 0 → 1, partial 1 → 0" in text
    assert "케이스 통과 1/2 → 2/2 · 제거 1 → 0" in text and "rb_push: partial → complete · 통과 1/2 → 2/2" in text
    assert "test_rb_push_full: 제거 → 통과 (A: new → fail → dropped [assertion_failed] / B: new → fail → new → pass) · 본문 다름" in text
    assert "그대로: 1개" in text and "수정한 실패 분류: assertion_failed 1\n" in text and "빌드 4 (같음)" in text
    assert "LLM 호출 6 (같음): analyzer 1, fixer 1, note_keeper 1, reviewer 1, test_writer 2" in text


@pytest.mark.libclang
def test_diff_against_replay_and_changed_source(world):
    a = _gen(world, _fix("fix_case", FULL_OK))
    replay = replay_run(make_context(world.home, "sample"), a, backend=world.be).rundir.path
    d = diff_runs(load_run(a), load_run(replay))
    assert d["relation"] == "b_replays_a" and d["inputs"]["model"] == ["fake-model", "replay"]
    assert d["functions"][0]["cases"] == [] and d["functions"][0]["unchanged"] == 2
    text = render_diff(d)
    assert "B는 A의 replay입니다 (기록된 응답만)" in text and "rb_push: complete · 통과 2/2" in text and "그대로: 2개" in text

    aj = replay / "analysis" / "rb_push.json"
    data = json.loads(aj.read_text(encoding="utf-8"))
    data["function"]["source"] += "\n// 바뀐 본문"
    aj.write_text(json.dumps(data), encoding="utf-8")
    assert diff_runs(load_run(a), load_run(replay))["inputs"]["source_changed"] == ["rb_push"]


@pytest.mark.libclang
def test_diff_cli(world):
    a = _gen(world, _fix("drop_case"))
    b = _gen(world, _fix("fix_case", FULL_OK))
    cli = CliRunner()
    base = ["--home", str(world.home), "--product", "sample"]
    out = cli.invoke(app, ["diff", a.name, b.name, *base])
    assert out.exit_code == 0, out.output
    assert f"실행 비교: A {a.name} → B {b.name}" in out.output and "없음 (같은 설정·규칙·프롬프트·대상 함수)" in out.output
    js = cli.invoke(app, ["diff", a.name, b.name, "--json", *base])
    assert json.loads(js.output)["metrics"]["b"]["results"] == {"complete": 1}
    default = cli.invoke(app, ["diff", *base])  # 직전 실행과 최신 실행
    assert default.exit_code == 0 and a.name in default.output and b.name in default.output
    missing = cli.invoke(app, ["diff", "latest~5", *base])
    assert missing.exit_code == 2 and "2개뿐" in missing.output
