"""genut gen을 CLI로 끝까지: FakeProvider 스크립트 + 실제 cmake·컴파일러·gtest (설계 13.1 7단계 e2e).

실제 단정 실패와 실제 세그폴트를 Fixer가 고치고, 통과 케이스로 최종 확인한 뒤 프로덕트 트리에 설치한다.
"""

import json
import shutil

import pytest
from typer.testing import CliRunner

from conftest import SAMPLE
from genut.cli import app
from genut.state import CaseStatus, RunState

pytestmark = [pytest.mark.integration, pytest.mark.libclang]

OK = 'struct rb rb;\nunsigned char buf[8];\nrb_init(&rb, buf, sizeof(buf));\nEXPECT_EQ(rb_push(&rb, "ab", 2), RB_OK);'
FULL = 'struct rb rb;\nunsigned char buf[4];\nrb_init(&rb, buf, 4);\nEXPECT_EQ(rb_push(&rb, "abcdef", 6), {});'
BAD_BUF = 'struct rb rb = {};\nrb.buf = (unsigned char *)16;\nrb.cap = 8;\nEXPECT_EQ(rb_push(&rb, "a", 1), RB_OK);'
NULL = 'EXPECT_EQ(rb_push(NULL, "a", 1), RB_EINVAL);'


def _cases(*items):
    return {"cases": [{"name": n, "purpose": f"{n} 확인", "body": b} for n, b in items], "helpers": []}


def _fix(body, evidence=""):
    return {"strategy": "fix_case", "reason": "고침", "body": body, "evidence": evidence, "suspected_bug": False, "helpers": []}


def test_gen_end_to_end(tmp_path):
    root, home = tmp_path / "proj", tmp_path / "home"
    shutil.copytree(SAMPLE, root)
    cli = CliRunner()
    assert cli.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert cli.invoke(app, ["product", "add", "sample", "--root", str(root), "--home", str(home)]).exit_code == 0
    script = tmp_path / "script.json"
    script.write_text(json.dumps({
        "analyzer": [{"testable": True, "reason": "", "cautions": ["rb_init으로 초기화"], "recommended": []}],
        "test_writer": [
            _cases(("test_rb_push_ok", OK)),
            _cases(("test_rb_push_full", FULL.format("RB_OK")), ("test_rb_push_bad_buf", BAD_BUF), ("test_rb_push_null", NULL)),
        ],
        "fixer": [_fix(FULL.format("RB_ENOSPC"), ":34 RB_ENOSPC"), _fix(NULL.replace("NULL", "NULL ")), _fix(NULL + "\n// 다시")],
    }), encoding="utf-8")
    base = ["--home", str(home), "--product", "sample"]
    sets = ["--set", "llm.provider=fake", "--set", f"llm.fake.script={script}", "--set", "gtest.link_targets=[rb,rbslot]",
            "--set", "coverage.enabled=false"]  # 수정 루프만 본다. 커버리지는 아래 테스트
    r = cli.invoke(app, ["gen", "--file", str(root / "src" / "ring_buffer.c"), "--func", "rb_push", *base, *sets])
    assert r.exit_code == 0, r.output
    assert "[3/8] 사전검증" in r.output and "[6/8] 수정" in r.output

    run = sorted((home / "products" / "sample" / "runs").iterdir())[-1]
    st = RunState.load(run / "state.json")
    fs = st.functions["rb_push"]
    status = {c.name: c.status for c in fs.cases}
    assert status["test_rb_push_ok"] == CaseStatus.PASS and status["test_rb_push_full"] == CaseStatus.PASS
    assert status["test_rb_push_bad_buf"] == CaseStatus.PASS and status["test_rb_push_null"] == CaseStatus.PASS
    bad = fs.case("test_rb_push_bad_buf")
    assert "crash" in [h.status for h in bad.history] and bad.last_failure.category == "crash"
    assert "-28" in fs.case("test_rb_push_full").last_failure.observed  # 실제 gtest가 낸 실제값
    assert fs.result == "complete" and fs.final_check == "ok"
    installed = root / "tests" / "generated" / "ring_buffer_rb_push_test.cc"
    assert installed.exists() and installed.read_text(encoding="utf-8").count("TEST_F(") == 4
    assert (root / "tests" / "generated" / "CMakeLists.txt").exists()

    show = cli.invoke(app, ["show", "latest", *base])
    assert show.exit_code == 0 and "rb_push: complete" in show.output
    view = cli.invoke(app, ["view", "latest", "--no-open", *base])
    assert view.exit_code == 0 and (run / "view.html").exists()
    again = cli.invoke(app, ["finalize", "latest", *base])
    assert again.exit_code == 0 and "최종 확인 ok" in again.output


def test_gen_reaches_full_line_coverage(tmp_path):
    """sample_project의 rb_push가 커버리지 라운드로 라인 커버리지 100%에 도달한다 (설계 13.1 8단계)."""
    root, home = tmp_path / "proj", tmp_path / "home"
    shutil.copytree(SAMPLE, root)
    cli = CliRunner()
    assert cli.invoke(app, ["product", "add", "sample", "--root", str(root), "--home", str(home)]).exit_code == 0
    full = 'struct rb rb;\nunsigned char buf[4];\nrb_init(&rb, buf, sizeof(buf));\nEXPECT_EQ(rb_push(&rb, "abcdef", 6), RB_ENOSPC);'
    nomem = 'struct rb rb;\nunsigned char buf[8];\nrb_init(&rb, buf, sizeof(buf));\nrb.head = 4;\nrb.tail = 8;\nEXPECT_EQ(rb_push(&rb, "ab", 2), RB_ENOMEM);'
    script = tmp_path / "script.json"
    script.write_text(json.dumps({
        "analyzer": [{"testable": True, "reason": "", "cautions": [], "recommended": []}],
        "test_writer": [_cases(("test_rb_push_ok", OK)), _cases(("test_rb_push_null", NULL)),
                        _cases(("test_rb_push_full", full), ("test_rb_push_nomem", nomem))],
    }), encoding="utf-8")
    r = cli.invoke(app, ["gen", "--file", str(root / "src" / "ring_buffer.c"), "--func", "rb_push", "--home", str(home), "--product", "sample",
                         "--set", "llm.provider=fake", "--set", f"llm.fake.script={script}", "--set", "gtest.link_targets=[rb,rbslot]"])
    assert r.exit_code == 0, r.output
    assert "[7/8] 커버리지" in r.output and "미커버 :33 :34 :38" in r.output

    run = sorted((home / "products" / "sample" / "runs").iterdir())[-1]
    fs = RunState.load(run / "state.json").functions["rb_push"]
    assert fs.coverage.percent == 100.0 and fs.coverage.total == 13 and fs.result == "complete"
    assert {c.name for c in fs.cases if c.origin == "coverage"} == {"test_rb_push_full", "test_rb_push_nomem"}
    assert list(run.glob("coverage/m0/rb_push.coverage.info"))  # gcov 결과를 lcov로 남긴다
    prompt = next(p for p in (run / "prompts").iterdir() if p.name.endswith("test_writer-coverage.md")).read_text(encoding="utf-8")
    assert "조건 (slot == NULL) 참 (:37) → RB_ENOMEM 반환" in prompt
