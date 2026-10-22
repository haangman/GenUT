from genut.knowledge import KnowledgeStore, normalize_key, parse_notes, render_notes
from genut.rules import load_rules, sections, strip_comments


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_rules_layering_and_extraction(tmp_path):
    home = tmp_path / "h"
    pdir = tmp_path / "h" / "products" / "a"
    _write(home / "rules" / "common.md", "# 공통\n\n## 반드시\n- 이름 규칙\n<!-- 안내 주석 -->\n\n## 리뷰에서 거부할 패턴\n- `EXPECT_TRUE\\(true\\)`\n")
    _write(home / "rules" / "kunit.md", "## 반드시 스텁\n- `readl` `*_hw_*`\n")
    _write(pdir / "rules" / "kunit.md", "## 반드시 스텁\n- `foo_reset`\n")
    r = load_rules(home, pdir, "kunit")
    assert [f.name for f in r.files] == ["common.md", "kunit.md", "kunit.md"]
    assert r.is_must_stub("readl") and r.is_must_stub("foo_hw_init") and r.is_must_stub("foo_reset")
    assert not r.is_must_stub("memcpy")
    assert r.compiled_rejects()[0].search("EXPECT_TRUE(true);")
    block = r.prompt_block()
    assert "공통 규칙" in block and "이 프로덕트 규칙" in block
    assert "안내 주석" not in block


def test_sections_and_comments():
    md = "## A\nx\n## B\ny\n## A\nz\n"
    assert sections(md) == {"A": "x\nz\n", "B": "y\n"}
    assert strip_comments("a<!-- b -->c") == "ac"


def test_notes_roundtrip():
    md = render_notes("t", parse_notes("- 노트 하나 <!-- genut id=aa key=kk runs=r1 -->\n", "global"))
    notes = parse_notes(md, "global")
    assert notes[0].id == "aa" and notes[0].runs == ["r1"]


def test_normalize_key_ignores_identifiers_and_numbers():
    a = normalize_key("`foo_init`은 42를 반환해야 한다 (runs: x)", {"foo_init"})
    b = normalize_key("`bar_init`은 7를 반환해야 한다", {"bar_init"})
    assert a == b


def test_note_scope_rules_and_promotion(tmp_path):
    home = tmp_path / "h"
    pa = home / "products" / "alpha"
    pb = home / "products" / "beta"
    ka = KnowledgeStore(home, pa, "alpha")
    # 프로덕트 심볼이 있으면 global을 제안해도 프로덕트로 강등
    n = ka.add_note("`foo_init`은 `foo_hw_reset` 스텁이 0이어야 한다", "global", "r1", {"foo_init", "foo_hw_reset"})
    assert n.scope == "product"
    # 일반 노트는 공통 후보로
    text = "KUnit에서 kunit_kzalloc 결과를 구조체 포인터 필드에 넣지 않으면 null_deref가 난다"
    n2 = ka.add_note(text, "global", "r2", {"foo_init"})
    assert n2.scope == "candidate"
    assert ka.notes("global") == []
    kb = KnowledgeStore(home, pb, "beta")
    kb.add_note(text, "global", "r3", {"bar_init"})
    glob = kb.notes("global")
    assert len(glob) == 1 and sorted(glob[0].products) == ["alpha", "beta"]
    # 다른 프로덕트 지식은 읽지 않는다
    assert all("foo_init" not in x.text for x in kb.notes("product"))
    block = kb.prompt_block(1000, lambda s: len(s) // 3)
    assert "공통 지식" in block and "foo_init" not in block


def test_promote_demote(tmp_path):
    home = tmp_path / "h"
    k = KnowledgeStore(home, home / "products" / "a", "a")
    n = k.add_note("일반적인 사실 하나", "global", "r1", set())
    k.promote(n.id)
    assert [x.id for x in k.notes("global")] == [n.id]
    k.demote(n.id)
    assert k.notes("global") == [] and k.notes("product")[0].id == n.id


def test_fix_examples_and_suspected_bugs(tmp_path):
    home = tmp_path / "h"
    k = KnowledgeStore(home, home / "products" / "a", "a")
    k.add_fix_example("missing_include", "unknown type name 'u8'", "a", "#include <linux/types.h>", "r1", set())
    k.add_fix_example("missing_include", "unknown type name 'u8'", "a", "b", "r2", set())
    ex = k.fix_examples("missing_include")
    assert len(ex) == 1 and ex[0]["runs"] == ["r1", "r2"]
    k.add_suspected_bug("rb_push", "test_rb_push_null", "NULL 검사 누락", "h1", "r1")
    assert k.suspected_bugs("rb_push", "h1")[0]["case"] == "test_rb_push_null"
    assert k.suspected_bugs("rb_push", "h2") == []  # 본문이 바뀌면 무효
