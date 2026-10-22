"""축적 지식: 노트, 수정 사례, 소스 버그 의심 목록 (설계 11.5).

노트 파일은 사람이 편집하는 md다. 노트 하나는 '- ' 로 시작하는 한 줄이며,
줄 끝의 HTML 주석에 메타데이터(id, key, cat, strategy, runs, products)를 둔다.

    - KUnit에서 ... null_deref가 난다 <!-- genut id=ab12 key=... cat=crash runs=r1,r2 products=alpha -->

범위 규칙:
1. 노트에 프로덕트 루트 아래 경로, 프로덕트 분석에서 나온 심볼·타입 이름, 프로덕트 설정값이 있으면 무조건 프로덕트 범위
2. 공통 노트는 먼저 프로덕트의 "공통 후보"로 저장. 다른 프로덕트에서도 같은 정규화 키
   (실패 분류 + 전략 + 식별자·경로·숫자를 자리표시자로 바꾼 문장)가 나오면
   auto_promote_after_products 기준으로 공통으로 승격
읽을 때는 공통 → 이 프로덕트(후보 포함) 순이며 다른 프로덕트의 노트는 프롬프트에 넣지 않는다.
다른 프로덕트 파일은 승격 판정과 `knowledge list --candidates`의 집계에만 읽는다.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from genut.paths import is_relative_to
from genut.tomlio import load_toml

if TYPE_CHECKING:
    from genut.analysis.model import FunctionInfo

_META = re.compile(r"\s*<!--\s*genut\s+(.*?)\s*-->\s*$")
# 한국어 조사가 바로 붙으므로(foo.c를) 경로·식별자 경계는 ASCII로만 본다
_PATH_TOKEN = re.compile(r"[\w.\-/\\:]*[/\\][\w.\-/\\]+|[\w.\-]+\.(?:c|h|cc|cpp|cxx|hh|hpp|hxx|inc|S|sh|py|cmake|txt)\b", re.ASCII)
CANDIDATE_HEADER = "## 공통 후보"
# product.toml에서 범위 판정에 쓰지 않는 키 (프로덕트 고유 값이 아니라 선택지)
_GENERIC_KEYS = {"backend", "crash_backtrace", "coverage_opt_level", "static_access", "sanitizers", "cmake_configure_args"}
_PRODUCT_SECTIONS = ("product", "gtest", "kunit")


@dataclass
class Note:
    text: str
    scope: str  # global | product | candidate
    id: str = ""
    key: str = ""
    category: str = ""
    strategy: str = ""
    runs: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)
    # 저장할 때만 채운다 (파일에 남지 않음)
    matched: list[str] = field(default_factory=list, compare=False)  # 프로덕트로 강등시킨 식별자·경로
    promoted: bool = field(default=False, compare=False)

    def render(self) -> str:
        meta = f"id={self.id} key={self.key}"
        if self.category:
            meta += f" cat={self.category}"
        if self.strategy:
            meta += f" strategy={self.strategy}"
        if self.runs:
            meta += " runs=" + ",".join(self.runs)
        if self.products:
            meta += " products=" + ",".join(self.products)
        return f"- {self.text} <!-- genut {meta} -->"


def message_pattern(text: str, identifiers: Iterable[str] = ()) -> str:
    """식별자·경로·숫자·따옴표 안 내용을 자리표시자로 바꾼 문장. 다른 프로덕트의 같은 노트·같은 오류 메시지를 맞춰 본다."""
    t = text.lower()
    t = re.sub(r"\(runs?:[^)]*\)", "", t)
    t = re.sub(r"`[^`]*`", "<x>", t)
    t = re.sub(r"'[^']*'|\"[^\"]*\"|‘[^’]*’|“[^”]*”", "<x>", t)  # gcc는 ‘ ’로 감싼다
    t = re.sub(r"[\w./-]+\.(c|h|cc|cpp|hpp)\b", "<path>", t)
    for ident in sorted(set(identifiers), key=len, reverse=True):
        if len(ident) > 2:
            t = re.sub(rf"(?<![a-z0-9_]){re.escape(ident.lower())}(?![a-z0-9_])", "<sym>", t)
    t = re.sub(r"(?<![a-z0-9_])(0x[0-9a-f]+|\d+)(?![a-z0-9_])", "<n>", t)
    return re.sub(r"\s+", " ", t).strip(" .")


def normalize_key(text: str, identifiers: Iterable[str] | None = None, category: str = "", strategy: str = "") -> str:
    """정규화 키: 실패 분류 + 전략 + 자리표시자로 바꾼 문장의 해시."""
    basis = f"{category}|{strategy}|{message_pattern(text, identifiers or ())}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]


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
        cat, strategy = meta.get("cat", ""), meta.get("strategy", "")
        notes.append(
            Note(
                text=text,
                scope=scope,
                id=meta.get("id") or _note_id(text),
                key=meta.get("key") or normalize_key(text, category=cat, strategy=strategy),
                category=cat,
                strategy=strategy,
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


# ------------------------------------------------------------------ 범위 판정
def _under(path: Path, roots: Iterable[Path]) -> bool:
    return any(is_relative_to(path, r) for r in roots)


def function_identifiers(fn: FunctionInfo, root: Path | None = None, shared: Iterable[Path] = ()) -> set[str]:
    """함수 분석에서 나온 프로덕트 심볼·타입 이름. root가 있으면 프로덕트 트리(공용 헤더 shared 제외)에 선언된 것만 센다.

    선언 위치를 모르는 것은 프로덕트 것으로 본다 (공통으로 잘못 가는 것보다 프로덕트에 남는 편이 안전하다).
    """
    shared = list(shared)

    def product_decl(file: str) -> bool:
        if root is None or not file:
            return True
        p = Path(file)
        return is_relative_to(p, root) and not _under(p, shared)

    ids = {fn.name, fn.key, fn.class_name, fn.namespace}
    if fn.owner is not None:
        ids.add(fn.owner.name)
    ids |= {c.name for c in fn.calls if not c.indirect and (product_decl(c.decl_file) if c.decl_file else product_decl(c.def_file))}
    ids |= {g.name for g in fn.globals if product_decl(g.decl_file)}
    ids |= {t.name.split()[-1] for t in fn.types if t.name and product_decl(t.file)}
    ids |= {c for c in fn.constants if product_decl(fn.constant_files.get(c, ""))}
    return {i for i in ids if i and len(i) > 2}


def config_identifiers(product_toml: Path) -> set[str]:
    """product.toml의 프로덕트 고유 값 (이름, 경로, 링크 타깃, 스크립트 등). 경로면 파일 이름도 넣는다."""
    data = load_toml(product_toml)
    out: set[str] = set()

    def walk(key: str, v: Any) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                walk(k, x)
        elif isinstance(v, list):
            for x in v:
                walk(key, x)
        elif isinstance(v, str) and key not in _GENERIC_KEYS and not key.endswith("_pattern"):
            s = v.strip()
            if len(s) > 2:
                out.add(s)
                base = re.split(r"[/\\]", s.rstrip("/\\"))[-1]
                if base != s and len(base) > 2:
                    out.add(base)

    for sec in _PRODUCT_SECTIONS:
        walk(sec, data.get(sec, {}))
    return out


@dataclass
class ProductFacts:
    """범위 검증 규칙 1에 쓰는 프로덕트 고유 정보."""

    identifiers: set[str] = field(default_factory=set)  # 분석에서 나온 심볼·타입 이름 + 프로덕트 설정값 + 대상 파일 이름
    root: Path | None = None  # 이 아래 경로가 노트에 나오면 프로덕트
    shared: list[Path] = field(default_factory=list)  # root 아래지만 공용인 경로 (커널 include 등)

    @classmethod
    def for_product(cls, root: Path, product_toml: Path, kernel_src: str | None = None) -> ProductFacts:
        """루트 경로와 product.toml 값. KUnit(kernel_src가 None이 아님)이면 커널 트리의 include·arch는 공용 API로 본다."""
        shared: list[Path] = []
        if kernel_src is not None:
            kernel = (root / kernel_src).resolve() if kernel_src else root.resolve()
            shared = [kernel / "include", kernel / "arch"]
        return cls(config_identifiers(product_toml), root, shared)

    @classmethod
    def of(cls, facts: ProductFacts | Iterable[str] | None) -> ProductFacts:
        if isinstance(facts, ProductFacts):
            return facts
        return cls(identifiers=set(facts or ()))

    def mentions(self, text: str) -> list[str]:
        """노트에 나온 프로덕트 고유 정보. 비어 있으면 공통이 될 수 있다."""
        found: list[str] = []
        norm = text.replace("\\", "/")
        for ident in sorted(self.identifiers, key=len, reverse=True):
            if len(ident) <= 2:
                continue
            i = ident.replace("\\", "/")
            flags = re.I if "/" in i else 0
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(i)}(?![A-Za-z0-9_])", norm, flags) and not any(i in f for f in found):
                found.append(ident)
        if self.root is not None:
            for tok in _PATH_TOKEN.findall(text):
                tok = tok.strip(".:,;`'\"()<>")
                if not tok or tok in found or ".." in Path(tok).parts:
                    continue
                p = Path(tok)
                try:
                    hit = is_relative_to(p, self.root) if p.is_absolute() else (self.root / p).exists() and not _under(self.root / p, self.shared)
                except OSError:
                    hit = False
                if hit:
                    found.append(tok)
        return found


# ------------------------------------------------------------------ store
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

    def ensure_file(self, scope: str) -> Path:
        """편집기로 열 파일. 없으면 빈 머리만 써 둔다."""
        if scope == "global":
            if not self.global_md.exists():
                self._write_global([])
            return self.global_md
        if self.product_md is None:
            raise ValueError("프로덕트가 없어 프로덕트 지식 파일을 열 수 없습니다.")
        if not self.product_md.exists():
            self._write_product([], [])
        return self.product_md

    # ----------------------------------------------------------- prompts
    def prompt_block(self, token_budget: int, count: Callable[[str], int]) -> str:
        """공통 → 이 프로덕트(공통 후보 포함) 순으로 토큰 예산 안에서 넣는다. 다른 프로덕트 지식은 읽지 않는다.

        각 묶음 안에서는 여러 실행에서 확인된 노트, 그다음 최근 노트부터 넣는다.
        """
        glob = self.notes("global")
        seen = {n.key for n in glob}
        own = [n for n in self.notes("product") + self.notes("candidate") if n.key not in seen]
        parts: list[str] = []
        used = 0
        for title, notes in (("공통 지식", glob), ("이 프로덕트 지식", own)):
            ranked = sorted(enumerate(notes), key=lambda p: (-len(p[1].runs), -p[0]))
            lines = []
            for _, n in ranked:
                line = f"- {n.text}"
                c = count(line)
                if used + c > token_budget:
                    continue
                lines.append(line)
                used += c
            if lines:
                parts.append(f"### {title}\n" + "\n".join(lines))
        return "\n\n".join(parts)

    # ------------------------------------------------------------- write
    def add_note(
        self,
        text: str,
        proposed_scope: str,
        run_id: str,
        facts: ProductFacts | Iterable[str] | None = None,
        auto_promote_after: int = 2,
        category: str = "",
        strategy: str = "",
    ) -> Note:
        """Note Keeper가 낸 노트를 범위 규칙에 따라 저장한다. 저장된(또는 합쳐진) 노트를 돌려준다."""
        facts = ProductFacts.of(facts)
        text = " ".join(text.split())
        matched = facts.mentions(text)
        key = normalize_key(text, facts.identifiers, category, strategy)
        scope = "candidate" if proposed_scope == "global" and not matched else "product"
        if self.product_md is None:  # 프로덕트 없이 쓰는 경우(테스트·도구)는 공통 후보 단계를 건너뛸 수 없으므로 저장하지 않는다
            raise ValueError("프로덕트가 없어 노트를 저장할 수 없습니다.")
        if scope == "candidate":
            glob = self.notes("global")
            g = next((n for n in glob if n.key == key), None)
            if g is not None:  # 이미 공통이면 근거만 더한다
                _add_run(g, f"{self.product_name}/{run_id}", self.product_name)
                self._write_global(glob)
                return g
        product, cands = self.notes("product"), self.notes("candidate")
        pool = product if scope == "product" else cands
        note = next((n for n in pool if n.key == key), None)
        if note is None:
            note = Note(text=text, scope=scope, id=_note_id(text), key=key, category=category, strategy=strategy, products=[self.product_name])
            pool.append(note)
        _add_run(note, run_id, self.product_name)
        self._write_product(product, cands)
        note.matched = matched
        if scope == "candidate":
            note.promoted = self._maybe_promote(note, auto_promote_after)
        return note

    def _same_key_elsewhere(self, key: str) -> dict[str, Note]:
        """다른 프로덕트의 공통 후보 중 같은 키. 승격 판정과 후보 집계에만 쓴다(프롬프트에는 넣지 않음)."""
        out: dict[str, Note] = {}
        root = self.home / "products"
        if not root.exists():
            return out
        for pdir in sorted(root.iterdir()):
            md = pdir / "knowledge" / "product.md"
            if pdir.name == self.product_name or not md.exists():
                continue
            hit = next((n for n in parse_notes(md.read_text(encoding="utf-8"), "product") if n.scope == "candidate" and n.key == key), None)
            if hit is not None:
                out[pdir.name] = hit
        return out

    def _maybe_promote(self, note: Note, threshold: int) -> bool:
        """같은 키의 공통 후보가 threshold개 프로덕트에 있으면 공통으로 승격한다."""
        others = self._same_key_elsewhere(note.key)
        if len(others) + 1 < threshold:
            return False
        glob = self.notes("global")
        if any(n.key == note.key for n in glob):
            return True
        runs = [f"{self.product_name}/{r}" for r in note.runs] + [f"{p}/{r}" for p, n in others.items() for r in n.runs]
        glob.append(Note(text=note.text, scope="global", id=note.id, key=note.key, category=note.category, strategy=note.strategy,
                         runs=runs, products=sorted({self.product_name, *others})))
        self._write_global(glob)
        return True

    def candidate_groups(self) -> dict[str, list[tuple[Note, list[str]]]]:
        """genut knowledge list --candidates: 이 프로덕트의 공통 후보를 실패 분류별로 묶고, 같은 키가 있는 프로덕트를 붙인다."""
        groups: dict[str, list[tuple[Note, list[str]]]] = {}
        glob = {n.key for n in self.notes("global")}
        for n in self.notes("candidate"):
            if n.key in glob:
                continue
            products = sorted({self.product_name, *self._same_key_elsewhere(n.key)})
            groups.setdefault(n.category, []).append((n, products))
        return dict(sorted(groups.items()))

    def find(self, note_id: str) -> Note | None:
        for n in self.notes("global") + self.notes("product") + self.notes("candidate"):
            if n.id == note_id:
                return n
        return None

    def promote(self, note_id: str) -> Note:
        product, cands = self.notes("product"), self.notes("candidate")
        for pool in (cands, product):
            for n in pool:
                if n.id == note_id:
                    pool.remove(n)
                    self._write_product(product, cands)
                    glob = self.notes("global")
                    n.scope = "global"
                    if not any(g.key == n.key for g in glob):
                        n.runs = [r if "/" in r else f"{self.product_name}/{r}" for r in n.runs]
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

    def fix_examples(self, category: str, message: str = "", limit: int = 3, identifiers: Iterable[str] = ()) -> list[dict]:
        """같은 분류의 과거 성공 수정. 같은 메시지 패턴 → 프로덕트 사례 → 여러 번 쓰인 사례 → 최근 순."""
        sig = normalize_key(category + message, identifiers) if message else ""
        items: list[tuple[tuple, dict]] = []
        for scope_rank, p in enumerate(reversed(self._fix_paths())):  # 0: 프로덕트, 1: 공통
            if not p.exists():
                continue
            loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or []
            for idx, it in enumerate(loaded):
                if it.get("category") == category:
                    items.append(((it.get("key") != sig if sig else True, scope_rank, -len(it.get("runs", [])), -idx), it))
        return [it for _, it in sorted(items, key=lambda x: x[0])][:limit]

    def add_fix_example(self, category: str, message: str, before: str, after: str, run_id: str,
                        facts: ProductFacts | Iterable[str] | None = None, identifiers: Iterable[str] | None = None) -> None:
        """성공한 수정을 남긴다. 범위는 facts(프로덕트 고유 정보)로, 메시지 패턴은 identifiers(대상 함수의 식별자, 조회 때와 같은 것)로 정한다."""
        facts = ProductFacts.of(facts)
        ids = set(identifiers) if identifiers is not None else facts.identifiers
        scope_product = bool(facts.mentions(before + "\n" + after + "\n" + message))
        path = self._fix_paths()[1 if scope_product and self.product_dir else 0]
        items = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else []
        items = items or []
        sig = normalize_key(category + message, ids)
        for it in items:
            if it.get("key") == sig:
                if run_id not in it.setdefault("runs", []):
                    it["runs"].append(run_id)
                break
        else:
            items.append({"key": sig, "category": category, "pattern": message_pattern(message, ids)[:200],
                          "message": message[:300], "before": before[-1200:], "after": after[-1200:], "runs": [run_id]})
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


def _add_run(note: Note, run: str, product: str) -> None:
    if run not in note.runs:
        note.runs.append(run)
    if product and product not in note.products:
        note.products.append(product)
