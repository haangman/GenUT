"""축적 지식: 노트, 수정 사례, 소스 버그 의심 목록 (설계 11.5).

노트 파일은 사람이 편집하는 md다. 노트 하나는 '- ' 로 시작하는 한 줄이며,
줄 끝의 HTML 주석에 메타데이터(id, key, runs, products)를 둔다.

    - KUnit에서 ... null_deref가 난다 <!-- genut id=ab12 key=... runs=r1,r2 products=alpha -->

범위 규칙:
1. 노트에 프로덕트 경로·심볼·설정값이 있으면 무조건 프로덕트 범위
2. 공통 노트는 먼저 프로덕트의 "공통 후보"로 저장. 다른 프로덕트에서도 같은
   정규화 키가 나오면 auto_promote_after_products 기준으로 공통으로 승격
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_META = re.compile(r"\s*<!--\s*genut\s+(.*?)\s*-->\s*$")
CANDIDATE_HEADER = "## 공통 후보"


@dataclass
class Note:
    text: str
    scope: str  # global | product | candidate
    id: str = ""
    key: str = ""
    runs: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)

    def render(self) -> str:
        meta = f"id={self.id} key={self.key}"
        if self.runs:
            meta += " runs=" + ",".join(self.runs)
        if self.products:
            meta += " products=" + ",".join(self.products)
        return f"- {self.text} <!-- genut {meta} -->"


def normalize_key(text: str, identifiers: set[str] | None = None) -> str:
    """정규화 키: 식별자·경로·숫자를 자리표시자로 바꾼 문장의 해시."""
    t = text.lower()
    t = re.sub(r"\(runs?:[^)]*\)", "", t)
    t = re.sub(r"`[^`]*`", "<x>", t)
    t = re.sub(r"[\w./-]+\.(c|h|cc|cpp|hpp)\b", "<path>", t)
    for ident in sorted(identifiers or set(), key=len, reverse=True):
        if len(ident) > 2:
            t = re.sub(rf"(?<![a-z0-9_]){re.escape(ident.lower())}(?![a-z0-9_])", "<sym>", t)
    t = re.sub(r"(?<![a-z0-9_])(0x[0-9a-f]+|\d+)(?![a-z0-9_])", "<n>", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:12]


def _note_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def parse_notes(md: str, default_scope: str) -> list[Note]:
    notes: list[Note] = []
    scope = default_scope
    for line in md.splitlines():
        if line.strip().startswith("## "):
            scope = "candidate" if line.strip() == CANDIDATE_HEADER else default_scope
            continue
        if not line.startswith("- "):
            continue
        body = line[2:]
        meta: dict[str, str] = {}
        m = _META.search(body)
        if m:
            body = body[: m.start()]
            for part in m.group(1).split():
                if "=" in part:
                    k, v = part.split("=", 1)
                    meta[k] = v
        text = body.strip()
        if not text:
            continue
        notes.append(
            Note(
                text=text,
                scope=scope,
                id=meta.get("id") or _note_id(text),
                key=meta.get("key") or normalize_key(text),
                runs=[r for r in meta.get("runs", "").split(",") if r],
                products=[p for p in meta.get("products", "").split(",") if p],
            )
        )
    return notes


def render_notes(title: str, notes: list[Note], candidates: list[Note] | None = None) -> str:
    lines = [f"# {title}", "", "GenUT가 쌓는 지식 노트입니다. 한 줄에 하나씩. 사람이 고쳐도 됩니다.", ""]
    lines += [n.render() for n in notes]
    if candidates is not None:
        lines += ["", CANDIDATE_HEADER, ""] + [n.render() for n in candidates]
    return "\n".join(lines).rstrip() + "\n"


class KnowledgeStore:
    def __init__(self, home: Path, product_dir: Path | None, product_name: str = "") -> None:
        self.home = home
        self.product_dir = product_dir
        self.product_name = product_name or (product_dir.name if product_dir else "")

    # ------------------------------------------------------------- paths
    @property
    def global_md(self) -> Path:
        return self.home / "knowledge" / "global.md"

    @property
    def product_md(self) -> Path | None:
        return self.product_dir / "knowledge" / "product.md" if self.product_dir else None

    def _read(self, path: Path | None, scope: str) -> list[Note]:
        if path is None or not path.exists():
            return []
        return parse_notes(path.read_text(encoding="utf-8"), scope)

    def notes(self, scope: str) -> list[Note]:
        if scope == "global":
            return self._read(self.global_md, "global")
        allp = self._read(self.product_md, "product")
        if scope == "candidate":
            return [n for n in allp if n.scope == "candidate"]
        return [n for n in allp if n.scope == "product"]

    def _write_product(self, product: list[Note], candidates: list[Note]) -> None:
        assert self.product_md is not None
        self.product_md.parent.mkdir(parents=True, exist_ok=True)
        self.product_md.write_text(render_notes(f"프로덕트 지식: {self.product_name}", product, candidates), encoding="utf-8")

    def _write_global(self, notes: list[Note]) -> None:
        self.global_md.parent.mkdir(parents=True, exist_ok=True)
        self.global_md.write_text(render_notes("공통 지식", notes), encoding="utf-8")

    # ----------------------------------------------------------- prompts
    def prompt_block(self, token_budget: int, count: callable) -> str:  # type: ignore[valid-type]
        """공통 → 프로덕트 순으로 토큰 예산 안에서 넣는다. 다른 프로덕트 지식은 읽지 않는다."""
        parts: list[str] = []
        used = 0
        for title, notes in (("공통 지식", self.notes("global")), ("이 프로덕트 지식", self.notes("product"))):
            lines = []
            for n in notes:
                line = f"- {n.text}"
                c = count(line)
                if used + c > token_budget:
                    break
                lines.append(line)
                used += c
            if lines:
                parts.append(f"### {title}\n" + "\n".join(lines))
        return "\n\n".join(parts)

    # ------------------------------------------------------------- write
    def add_note(self, text: str, proposed_scope: str, run_id: str, product_identifiers: set[str], auto_promote_after: int = 2) -> Note:
        """Note Keeper가 낸 노트를 범위 규칙에 따라 저장한다. 저장된 노트를 돌려준다."""
        mentions_product = _mentions(text, product_identifiers)
        key = normalize_key(text, product_identifiers)
        scope = "product" if (proposed_scope != "global" or mentions_product) else "candidate"
        product = self.notes("product")
        cands = self.notes("candidate")
        pool = product if scope == "product" else cands
        for n in pool:
            if n.key == key:
                if run_id not in n.runs:
                    n.runs.append(run_id)
                self._write_product(product, cands)
                return n
        note = Note(text=text.strip(), scope=scope, id=_note_id(text), key=key, runs=[run_id], products=[self.product_name])
        pool.append(note)
        self._write_product(product, cands)
        if scope == "candidate":
            self._maybe_promote(note, auto_promote_after)
        return note

    def _maybe_promote(self, note: Note, threshold: int) -> bool:
        """다른 프로덕트의 공통 후보에 같은 키가 있으면 승격한다."""
        products_with_key = {self.product_name}
        root = self.home / "products"
        if root.exists():
            for pdir in root.iterdir():
                if pdir.name == self.product_name:
                    continue
                md = pdir / "knowledge" / "product.md"
                if md.exists() and any(n.key == note.key for n in parse_notes(md.read_text(encoding="utf-8"), "product") if n.scope == "candidate"):
                    products_with_key.add(pdir.name)
        if len(products_with_key) < threshold:
            return False
        glob = self.notes("global")
        if not any(n.key == note.key for n in glob):
            glob.append(Note(text=note.text, scope="global", id=note.id, key=note.key, runs=note.runs, products=sorted(products_with_key)))
            self._write_global(glob)
        return True

    def promote(self, note_id: str) -> Note:
        product, cands = self.notes("product"), self.notes("candidate")
        for pool in (cands, product):
            for n in pool:
                if n.id == note_id:
                    pool.remove(n)
                    self._write_product(product, cands)
                    glob = self.notes("global")
                    n.scope = "global"
                    glob.append(n)
                    self._write_global(glob)
                    return n
        raise KeyError(note_id)

    def demote(self, note_id: str) -> Note:
        glob = self.notes("global")
        for n in glob:
            if n.id == note_id:
                glob.remove(n)
                self._write_global(glob)
                product, cands = self.notes("product"), self.notes("candidate")
                n.scope = "product"
                product.append(n)
                self._write_product(product, cands)
                return n
        raise KeyError(note_id)

    # ------------------------------------------------------ fix patterns
    def _fix_paths(self) -> list[Path]:
        paths = [self.home / "knowledge" / "fix_patterns.yaml"]
        if self.product_dir:
            paths.append(self.product_dir / "knowledge" / "fix_patterns.yaml")
        return paths

    def fix_examples(self, category: str, limit: int = 3) -> list[dict]:
        out: list[dict] = []
        for p in reversed(self._fix_paths()):  # 프로덕트 사례 먼저
            if p.exists():
                items = yaml.safe_load(p.read_text(encoding="utf-8")) or []
                out += [i for i in items if i.get("category") == category]
        return out[:limit]

    def add_fix_example(self, category: str, message: str, before: str, after: str, run_id: str, product_identifiers: set[str]) -> None:
        scope_product = _mentions(before + after + message, product_identifiers)
        path = self._fix_paths()[1 if scope_product and self.product_dir else 0]
        items = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else []
        items = items or []
        sig = normalize_key(category + message, product_identifiers)
        for it in items:
            if it.get("key") == sig:
                it.setdefault("runs", []).append(run_id)
                break
        else:
            items.append({"key": sig, "category": category, "message": message[:300], "before": before[-1200:], "after": after[-1200:], "runs": [run_id]})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(items[-200:], allow_unicode=True, sort_keys=False), encoding="utf-8")

    # ---------------------------------------------------- suspected bugs
    def _bugs_path(self) -> Path | None:
        return self.product_dir / "knowledge" / "suspected_bugs.md" if self.product_dir else None

    def suspected_bugs(self, function: str, body_hash: str) -> list[dict]:
        p = self._bugs_path()
        if p is None or not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^- `([^`]+)` / `([^`]+)`: (.*?) <!-- genut hash=(\w+) run=(\S+) -->$", line)
            if m and m.group(1) == function and m.group(4) == body_hash:
                out.append({"function": m.group(1), "case": m.group(2), "reason": m.group(3), "run": m.group(5)})
        return out

    def add_suspected_bug(self, function: str, case: str, reason: str, body_hash: str, run_id: str) -> None:
        p = self._bugs_path()
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        head = "" if p.exists() else "# 소스 버그 의심 목록\n\n함수 본문 해시가 바뀌면 그 항목은 무효로 보고 다시 시도합니다.\n\n"
        reason1 = reason.replace("\n", " ").strip()
        with p.open("a", encoding="utf-8") as f:
            f.write(head + f"- `{function}` / `{case}`: {reason1} <!-- genut hash={body_hash} run={run_id} -->\n")


def _mentions(text: str, identifiers: set[str]) -> bool:
    for ident in identifiers:
        if len(ident) > 2 and re.search(rf"(?<![\w]){re.escape(ident)}(?![\w])", text):
            return True
    return False
