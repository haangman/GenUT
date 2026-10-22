"""지식 노트 (설계 11.5, 13.1 13단계): 범위 검증, 공통 후보 → 자동 승격, 다른 프로덕트 지식 미로드, Note Keeper, knowledge 명령."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from conftest import SAMPLE, write_compile_db
from fake_backend import MarkerBackend
from genut.agents.note_keeper import FixRecord, NoteKeeperOutput, accept_notes, function_digest, has_lessons
from genut.analysis.model import CallSite, FunctionInfo, GlobalRef, TypeDef
from genut.cli import app
from genut.context import make_context
from genut.knowledge import KnowledgeStore, ProductFacts, config_identifiers, function_identifiers, normalize_key
from genut.llm.fake_provider import FakeProvider
from genut.models import FailureInfo
from genut.orchestrator import GenOptions, Orchestrator
from genut.paths import ensure_home_layout
from genut.products import add_product
from genut.state import CaseStatus, FunctionState, RunState, TestCase, UnreachableLine
from genut.tomlio import write_toml

KZALLOC = "KUnit에서 `kunit_kzalloc` 결과를 구조체 포인터 필드 2개에 넣지 않으면 null_deref가 난다"
KMALLOC = "KUnit에서 `kmalloc` 결과를 구조체 포인터 필드 3개에 넣지 않으면 null_deref가 난다"  # 정규화 키가 같다


@pytest.fixture
def tree(tmp_path):
    """프로덕트 둘과 소스 트리. alpha는 drivers/foo/foo.c, 링크 타깃 foolib."""
    home = tmp_path / "home"
    ensure_home_layout(home)
    roots = {}
    for name in ("alpha", "beta", "gamma"):
        root = tmp_path / name
        (root / "drivers" / "foo").mkdir(parents=True)
        (root / "drivers" / "foo" / "foo.c").write_text("int foo_init(void) { return 0; }\n", encoding="utf-8")
        (root / "include" / "linux").mkdir(parents=True)
        (root / "include" / "linux" / "slab.h").write_text("", encoding="utf-8")
        add_product(home, name, root, "kunit")
        roots[name] = root
    toml = home / "products" / "alpha" / "product.toml"
    write_toml(toml, {"product": {"name": "alpha", "root": str(roots["alpha"]), "backend": "kunit"},
                      "kunit": {"test_file": "drivers/foo/foo_kunit.c", "run_script": "scripts/run_foo.sh", "static_access": "include_source"},
                      "gtest": {"link_targets": ["foolib"], "test_file_pattern": "{src}_{func}_test.cc"}})
    stores = {n: KnowledgeStore(home, home / "products" / n, n) for n in roots}
    facts = {n: ProductFacts.for_product(r, home / "products" / n / "product.toml", kernel_src="") for n, r in roots.items()}
    return SimpleNamespace(home=home, roots=roots, ks=stores, facts=facts)


# ------------------------------------------------------------- 범위 판정
def test_config_identifiers_skip_choices(tree):
    ids = config_identifiers(tree.home / "products" / "alpha" / "product.toml")
    assert {"alpha", "foolib", "drivers/foo/foo_kunit.c", "foo_kunit.c", "scripts/run_foo.sh", "run_foo.sh"} <= ids
    assert not {"kunit", "include_source", "{src}_{func}_test.cc"} & ids  # 선택지·패턴은 프로덕트 고유 값이 아니다


def test_mentions_product_paths_symbols_and_settings(tree):
    f = tree.facts["alpha"]
    f.identifiers.add("foo_init")
    assert f.mentions("`foo_init`은 0을 반환한다") == ["foo_init"]
    assert f.mentions("drivers/foo/foo.c를 고치면 다시 분석한다") == ["drivers/foo/foo.c"]  # 루트 아래에 있는 경로
    assert f.mentions("테스트는 foolib을 링크해야 한다") == ["foolib"]
    assert f.mentions(f"빌드 로그는 {tree.roots['alpha'] / 'out'}에 남는다")  # 루트 아래 절대 경로
    # 커널 트리의 include는 공용 API, 루트에 없는 경로·선택지 이름은 프로덕트 정보가 아니다
    assert f.mentions("kzalloc을 쓰려면 include/linux/slab.h나 <linux/slab.h>가 필요하고 kunit 백엔드에서만 그렇다") == []
    assert f.mentions(KZALLOC) == []


def test_function_identifiers_count_only_product_declarations(tree):
    root = tree.roots["alpha"]
    fn = FunctionInfo(
        name="foo_init", signature="int foo_init(struct foo_dev *d)", return_type="int", file=str(root / "drivers/foo/foo.c"), start_line=1, end_line=9,
        calls=[CallSite(name="foo_hw_reset", line=2, decl_file=str(root / "drivers/foo/foo.h")),
               CallSite(name="kzalloc", line=3, decl_file=str(root / "include/linux/slab.h")),
               CallSite(name="memcpy", line=4, decl_file=str(root.parent / "sysroot/string.h"))],
        types=[TypeDef(name="struct foo_dev", kind="struct", text="struct foo_dev { int x; };\nenum foo_state { FOO_IDLE };", file=str(root / "drivers/foo/foo.h"))],
        constants={"FOO_IDLE": "0", "EINVAL": "22"},
        constant_files={"FOO_IDLE": str(root / "drivers/foo/foo.h"), "EINVAL": str(root / "include/uapi/errno.h")},
        globals=[GlobalRef(name="foo_count", decl_file=str(root / "drivers/foo/foo.c")), GlobalRef(name="jiffies", decl_file=str(root / "include/linux/jiffies.h"))],
    )
    ids = function_identifiers(fn, root, [root / "include"])
    assert ids == {"foo_init", "foo_hw_reset", "foo_dev", "FOO_IDLE", "foo_count"}
    assert {"kzalloc", "memcpy", "EINVAL", "jiffies"} <= function_identifiers(fn)  # 루트를 모르면 모두 프로덕트로 (안전한 쪽)


# ------------------------------------------------- 저장·승격·미로드
def test_product_identifier_note_never_goes_global(tree):
    f = tree.facts["alpha"]
    f.identifiers |= {"foo_init", "foo_hw_reset"}
    n = tree.ks["alpha"].add_note("`foo_init`은 `foo_hw_reset` 스텁이 0이어야 정상 경로로 간다", "global", "r1", f, category="crash")
    assert n.scope == "product" and n.matched == ["foo_hw_reset", "foo_init"]
    p = tree.ks["alpha"].add_note("drivers/foo/foo.c는 CONFIG_FOO가 켜져야 빌드된다", "global", "r1", f)
    assert p.scope == "product" and p.matched == ["drivers/foo/foo.c"]
    assert tree.ks["alpha"].notes("global") == [] and tree.ks["alpha"].notes("candidate") == []


def test_same_note_in_two_products_is_promoted(tree):
    ka, kb, kc = tree.ks["alpha"], tree.ks["beta"], tree.ks["gamma"]
    assert normalize_key(KZALLOC, category="crash", strategy="fix_case") == normalize_key(KMALLOC, category="crash", strategy="fix_case")
    a = ka.add_note(KZALLOC, "global", "r1", tree.facts["alpha"], 2, "crash", "fix_case")
    assert a.scope == "candidate" and not a.promoted and ka.notes("global") == []
    other = kb.add_note(KMALLOC, "global", "r2", tree.facts["beta"], 2, "assertion_failed", "fix_case")  # 분류가 다르면 같은 노트가 아니다
    assert not other.promoted and kb.notes("global") == []
    b = kb.add_note(KMALLOC, "global", "r3", tree.facts["beta"], 2, "crash", "fix_case")
    assert b.promoted
    [g] = kb.notes("global")
    assert g.text == KMALLOC and g.products == ["alpha", "beta"] and sorted(g.runs) == ["alpha/r1", "beta/r3"] and g.category == "crash"
    # 이미 공통이면 셋째 프로덕트는 후보를 만들지 않고 근거만 더한다
    c = kc.add_note(KZALLOC, "global", "r9", tree.facts["gamma"], 2, "crash", "fix_case")
    assert c.scope == "global" and kc.notes("candidate") == []
    assert kc.notes("global")[0].products == ["alpha", "beta", "gamma"] and "gamma/r9" in kc.notes("global")[0].runs
    # 승격된 노트는 한 번만 들어간다
    block = ka.prompt_block(2000, len)
    assert block.count("null_deref") == 1 and block.startswith("### 공통 지식")


def test_other_products_knowledge_is_not_loaded(tree):
    tree.facts["alpha"].identifiers.add("foo_init")
    tree.ks["alpha"].add_note("`foo_init`은 레지스터 맵을 먼저 채워야 한다", "product", "r1", tree.facts["alpha"])
    tree.ks["alpha"].add_note("ALPHA 후보: 픽스처마다 kunit_kzalloc을 다시 부른다", "global", "r1", tree.facts["alpha"])
    tree.ks["beta"].add_note("BETA 노트: 링 버퍼는 용량 0이면 EINVAL을 낸다", "product", "r2", tree.facts["beta"])
    block = tree.ks["beta"].prompt_block(5000, len)
    assert "BETA 노트" in block and "foo_init" not in block and "ALPHA" not in block
    own = tree.ks["alpha"].prompt_block(5000, len)
    assert "foo_init" in own and "ALPHA 후보" in own and "BETA" not in own  # 자기 공통 후보는 프로덕트 지식으로 넣는다


def test_prompt_block_prefers_confirmed_notes_within_budget(tree):
    k = tree.ks["beta"]
    k.add_note("한 번 본 노트", "product", "r1", tree.facts["beta"])
    k.add_note("여러 번 확인된 노트", "product", "r1", tree.facts["beta"])
    k.add_note("여러 번 확인된 노트", "product", "r2", tree.facts["beta"])
    k.add_note("아주 긴 노트 " + "x" * 400, "product", "r3", tree.facts["beta"])
    block = k.prompt_block(80, len)
    assert block.index("여러 번 확인된") < block.index("한 번 본") and "아주 긴" not in block  # 넘치는 노트는 건너뛴다


def test_candidate_groups_by_category(tree):
    tree.ks["alpha"].add_note(KZALLOC, "global", "r1", tree.facts["alpha"], 3, "crash", "fix_case")
    tree.ks["beta"].add_note(KMALLOC, "global", "r2", tree.facts["beta"], 3, "crash", "fix_case")
    tree.ks["beta"].add_note("EXPECT_STREQ는 NULL을 받으면 크래시한다", "global", "r2", tree.facts["beta"], 3)
    groups = tree.ks["beta"].candidate_groups()
    assert list(groups) == ["", "crash"]
    [(note, products)] = groups["crash"]
    assert note.text == KMALLOC and products == ["alpha", "beta"]


def test_fix_examples_prefer_same_pattern_then_product(tree):
    k = tree.ks["alpha"]
    k.add_fix_example("compile_error", "unknown type name ‘u8’", "u8 x;", "#include <linux/types.h>\nu8 x;", "r1", set())
    k.add_fix_example("compile_error", "implicit declaration of function ‘kzalloc’", "a", "b", "r1", set())
    k.add_fix_example("compile_error", "‘struct foo_dev’ has no member named ‘bar’", "d.bar = 1;", "d.baz = 1;", "r2", {"foo_dev"})
    assert (tree.home / "products" / "alpha" / "knowledge" / "fix_patterns.yaml").exists()  # 프로덕트 심볼이 든 사례는 프로덕트로
    same = k.fix_examples("compile_error", "unknown type name ‘s16’")
    assert same[0]["after"].startswith("#include <linux/types.h>") and same[0]["pattern"] == "unknown type name <x>"
    assert k.fix_examples("compile_error")[0]["before"] == "d.bar = 1;"  # 메시지가 없으면 프로덕트 사례 먼저
    seen = [e["message"] for e in tree.ks["beta"].fix_examples("compile_error", limit=5)]
    assert seen == ["implicit declaration of function ‘kzalloc’", "unknown type name ‘u8’"]  # 다른 프로덕트 사례는 안 보인다, 최근 것 먼저


# ------------------------------------------------------------ Note Keeper
def _run_state():
    fi = FailureInfo(category="assertion_failed", stage="run", summary="Expected rb_push(...) == RB_OK, actual -28")
    fs = FunctionState(name="rb_push", cases=[
        TestCase(name="test_rb_push_ok", target_function="rb_push", body="EXPECT_EQ(1, 1);", status=CaseStatus.PASS),
        TestCase(name="test_rb_push_full", target_function="rb_push", body="EXPECT_EQ(x, RB_ENOSPC);", status=CaseStatus.PASS, fixes_used=1,
                 previous_body="EXPECT_EQ(x, RB_OK);", last_failure=fi),
        TestCase(name="test_rb_push_bad", target_function="rb_push", body="BAD", status=CaseStatus.DROPPED, drop_reason="크래시가 다시 났습니다"),
    ], unreachable=[UnreachableLine(line=38, category="needs_stub_behavior", reason="rb_alloc_slot이 NULL이어야 함")])
    st = RunState(run_id="r1", source_file=str(SAMPLE / "src" / "ring_buffer.c"), backend="gtest", functions={"rb_push": fs})
    fixes = [FixRecord("rb_push", "test_rb_push_full", "assertion_failed", fi.summary, "fix_case", "가득 차면 RB_ENOSPC를 돌려준다 (:34)")]
    return st, fixes


def test_digest_and_lessons():
    st, fixes = _run_state()
    full = function_digest(st.functions["rb_push"], "rb_push", fixes)
    assert "- 첫 시도에 통과: test_rb_push_ok" in full and "  - 실패 assertion_failed: Expected" in full
    assert "  - 적용한 전략 fix_case: 가득 차면" in full and "EXPECT_EQ(x, RB_OK);" in full and "- test_rb_push_bad: 제거 — 크래시가" in full
    assert "- 미커버 :38 needs_stub_behavior" in full
    assert "EXPECT_EQ(x, RB_OK);" not in function_digest(st.functions["rb_push"], "rb_push", fixes, full=False)
    assert has_lessons(st, fixes)
    clean = RunState(run_id="r2", source_file="x.c", backend="gtest", functions={"f": FunctionState(name="f", cases=[
        TestCase(name="t", target_function="f", body="", status=CaseStatus.PASS)])})
    assert not has_lessons(clean, [])


def test_accept_notes_checks_evidence_category_strategy_and_limit():
    st, fixes = _run_state()
    out = NoteKeeperOutput.model_validate({"notes": [
        {"text": "가득 찬 링 버퍼에 쓰면  RB_ENOSPC가 나온다", "scope": "product", "category": "assertion_failed", "strategy": "fix_case", "evidence": "test_rb_push_full"},
        {"text": "근거 없는 노트", "scope": "global", "category": "", "strategy": "", "evidence": "다른 함수"},
        {"text": "x" * 301, "scope": "global", "category": "", "strategy": "", "evidence": "rb_push"},
        {"text": "가득 찬 링 버퍼에 쓰면 RB_ENOSPC가 나온다", "scope": "product", "category": "", "strategy": "", "evidence": "rb_push"},
        {"text": "크래시 케이스는 다시 쓰지 않는다", "scope": "global", "category": "made_up", "strategy": "retry", "evidence": "test_rb_push_bad"},
        {"text": "상한을 넘는 노트", "scope": "global", "category": "", "strategy": "", "evidence": "rb_push"},
    ]})
    res = accept_notes(out, st, fixes, {"rb_push": "rb_push"}, max_notes=2)
    assert [n.text for n in res.accepted] == ["가득 찬 링 버퍼에 쓰면 RB_ENOSPC가 나온다", "크래시 케이스는 다시 쓰지 않는다"]
    assert (res.accepted[1].category, res.accepted[1].strategy) == ("", "")  # 이번 실행에 없던 분류·사다리에 없는 전략은 비운다
    reasons = [why for _, why in res.rejected]
    assert reasons == ["근거가 이번 실행의 함수·케이스를 가리키지 않음", "300자를 넘음", "같은 노트", "실행당 상한 2개를 넘음"]


# ------------------------------------------------------- 오케스트레이터
OK = 'struct rb rb;\nunsigned char buf[8];\nrb_init(&rb, buf, 8);\nEXPECT_EQ(rb_push(&rb, "a", 1), RB_OK);'
FULL_BAD = 'EXPECT_EQ(rb_push(&rb, "abcdef", 6), RB_OK); // FAIL ACTUAL=-28'
FULL_OK = 'EXPECT_EQ(rb_push(&rb, "abcdef", 6), RB_ENOSPC);'
ANALYZED = {"testable": True, "reason": "", "cautions": [], "recommended": []}


def _cases(*items):
    return {"cases": [{"name": n, "purpose": n, "body": b} for n, b in items], "helpers": []}


FIX = {"strategy": "fix_case", "reason": "가득 차면 RB_ENOSPC", "body": FULL_OK, "evidence": ":34 RB_ENOSPC", "suspected_bug": False, "helpers": []}
NOTES = {"notes": [
    {"text": "`rb_push`는 남은 공간보다 길게 쓰면 RB_ENOSPC를 돌려준다", "scope": "global", "category": "assertion_failed", "strategy": "fix_case",
     "evidence": "test_rb_push_full"},
    {"text": "단정 실패를 고칠 때는 반환 지점의 조건을 먼저 확인하면 한 번에 고쳐진다", "scope": "global", "category": "assertion_failed",
     "strategy": "fix_case", "evidence": "rb_push / test_rb_push_full"},
    {"text": "근거 없는 노트", "scope": "global", "category": "", "strategy": "", "evidence": "nothing"},
]}


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    ensure_home_layout(home)
    add_product(home, "sample", SAMPLE, "gtest")
    add_product(home, "other", tmp_path, "gtest")
    be = MarkerBackend(write_compile_db(tmp_path / "cc.json"), tmp_path / "installed", [SAMPLE / "src" / "ring_buffer.c", SAMPLE / "src" / "slot.c"])
    return SimpleNamespace(home=home, be=be)


def _gen(world, script, opts=None):
    p = FakeProvider(script={"analyzer": [ANALYZED], **script})
    orch = Orchestrator(make_context(world.home, "sample"), SAMPLE / "src" / "ring_buffer.c", opts or GenOptions(functions=["rb_push"]), provider=p,
                        backend=world.be)
    return orch.run(), orch, p


def _events(orch, type_):
    return [e for e in map(json.loads, orch.rundir.trace_jsonl.read_text(encoding="utf-8").splitlines()) if e["type"] == type_]


@pytest.mark.libclang
def test_note_keeper_after_run_saves_scoped_notes(world):
    KnowledgeStore(world.home, world.home / "products" / "other", "other").add_note("OTHER 프로덕트의 비밀 노트", "product", "o1", set())
    script = {"test_writer": [_cases(("test_rb_push_ok", OK)), _cases(("test_rb_push_full", FULL_BAD))], "fixer": [FIX], "note_keeper": [NOTES]}
    st, orch, p = _gen(world, script)
    assert [c["tag"] for c in p.calls][-1] == "note_keeper" and st.stage != "notes"  # 실행 상태의 단계는 바꾸지 않는다
    assert [e["stage"] for e in _events(orch, "llm_call") if e["agent"] == "note_keeper"] == ["notes"]
    prompt = p.calls[-1]["messages"][1].content
    assert "## 실행 요약: rb_push" in prompt and "실패 assertion_failed" in prompt and "적용한 전략 fix_case" in prompt
    saved = {n.text: n for n in st.knowledge_notes}
    product = saved["`rb_push`는 남은 공간보다 길게 쓰면 RB_ENOSPC를 돌려준다"]
    assert (product.proposed, product.scope, product.matched) == ("global", "product", ["RB_ENOSPC", "rb_push"])  # 분석에서 나온 심볼이 있으면 프로덕트
    assert saved["단정 실패를 고칠 때는 반환 지점의 조건을 먼저 확인하면 한 번에 고쳐진다"].scope == "candidate"
    assert [e["reason"] for e in _events(orch, "note_rejected")] == ["근거가 이번 실행의 함수·케이스를 가리키지 않음"]
    assert len(_events(orch, "note_saved")) == 2 and orch.budget.tokens.get("(notes)", 0) > 0
    report = (orch.rundir.path / "final" / "report.md").read_text(encoding="utf-8")
    assert "## 지식 노트" in report and "[프로덕트] `rb_push`는" in report and "프로덕트 고유 정보 때문에 프로덕트로: RB_ENOSPC, rb_push" in report
    md = (world.home / "products" / "sample" / "knowledge" / "product.md").read_text(encoding="utf-8")
    assert "cat=assertion_failed strategy=fix_case" in md and "## 공통 후보" in md

    # 다음 실행: 이 프로덕트의 노트(후보 포함)가 Test Writer에 들어가고, 다른 프로덕트 노트는 들어가지 않는다
    st2, _, p2 = _gen(world, {"test_writer": [_cases(("test_rb_push_ok", OK))]})
    writer = next(c for c in p2.calls if c["tag"] == "test_writer")["messages"][1].content
    assert "## 지식 노트" in writer and "RB_ENOSPC를 돌려준다" in writer and "반환 지점의 조건" in writer
    assert all("OTHER" not in m.content for c in p2.calls for m in c["messages"])
    assert "note_keeper" not in [c["tag"] for c in p2.calls] and st2.knowledge_notes == []  # 배울 것이 없는 실행


@pytest.mark.libclang
def test_note_keeper_skipped_when_disabled(world):
    script = {"test_writer": [_cases(("test_rb_push_ok", OK)), _cases(("test_rb_push_full", FULL_BAD))], "fixer": [FIX], "note_keeper": [NOTES]}
    _, _, p = _gen(world, script, GenOptions(functions=["rb_push"], notes=False))  # genut eval·replay
    assert "note_keeper" not in [c["tag"] for c in p.calls]
    _, _, p = _gen(world, script, GenOptions(functions=["rb_push"], dry_run=True))
    assert "note_keeper" not in [c["tag"] for c in p.calls]
    assert not (world.home / "products" / "sample" / "knowledge" / "product.md").exists()


# ------------------------------------------------------------------- CLI
def test_knowledge_cli(tree, monkeypatch):
    cli = CliRunner()
    tree.facts["alpha"].identifiers.add("foo_init")
    ka = tree.ks["alpha"]
    prod = ka.add_note("`foo_init`은 레지스터 맵을 먼저 채워야 한다", "global", "r1", tree.facts["alpha"])
    path_note = ka.add_note("foolib을 링크해야 한다", "product", "r1", set())
    cand = ka.add_note(KZALLOC, "global", "r2", tree.facts["alpha"], 2, "crash", "fix_case")
    tree.ks["beta"].add_note("BETA 노트", "product", "b1", set())
    base = ["--home", str(tree.home), "--product", "alpha"]

    out = cli.invoke(app, ["knowledge", "list", *base])
    assert out.exit_code == 0, out.output
    assert "공통 지식 (0)" in out.output and "프로덕트 지식: alpha (2)" in out.output and f"{cand.id}  {KZALLOC}  [crash] 전략 fix_case" in out.output
    assert "BETA" not in out.output
    out = cli.invoke(app, ["knowledge", "list", "--candidates", *base])
    assert "[crash]" in out.output and "같은 노트가 있는 프로덕트 1개: alpha" in out.output

    refused = cli.invoke(app, ["knowledge", "promote", path_note.id, *base])
    assert refused.exit_code == 1 and "foolib" in refused.output  # product.toml 값이 든 노트는 --force 없이 올리지 않는다
    assert cli.invoke(app, ["knowledge", "promote", path_note.id, "--force", *base]).exit_code == 0
    assert cli.invoke(app, ["knowledge", "promote", cand.id, *base]).exit_code == 0
    glob = {n.id for n in ka.notes("global")}
    assert glob == {path_note.id, cand.id} and ka.notes("candidate") == [] and ka.notes("global")[0].runs == ["alpha/r1"]
    assert cli.invoke(app, ["knowledge", "list", "--scope", "global", "--home", str(tree.home)]).output.count("  ") >= 2  # 프로덕트 없이도 공통은 본다
    out = cli.invoke(app, ["knowledge", "demote", path_note.id, *base])
    assert out.exit_code == 0 and {n.id for n in ka.notes("product")} == {prod.id, path_note.id}
    assert cli.invoke(app, ["knowledge", "demote", "nope", *base]).exit_code == 1

    opened = []
    monkeypatch.setenv("EDITOR", "my-editor --wait")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(subprocess, "call", lambda argv: opened.append(argv))
    assert cli.invoke(app, ["knowledge", "edit", "--scope", "global", *base]).exit_code == 0
    assert cli.invoke(app, ["knowledge", "edit", *base]).exit_code == 0
    assert opened == [["my-editor", "--wait", str(tree.home / "knowledge" / "global.md")],
                      ["my-editor", "--wait", str(tree.home / "products" / "alpha" / "knowledge" / "product.md")]]
