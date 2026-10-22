"""libclang으로 함수 구조를 뽑는다 (설계 6.3).

시그니처, 파라미터 타입(구조체 필드까지), 호출 심볼, 전역 참조(읽기/쓰기), 반환 지점과 그 조건,
분기 영역(커버리지 도달 조건용), 열거형·매크로 상수, include 줄, #if 조건, static 여부.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from genut.analysis.compile_db import CompileEntry, builtin_include_args, compiler_target, guess_target
from genut.analysis.flag_sanitizer import FlagSanitizer
from genut.analysis.model import Branch, CallSite, ClassInfo, FunctionInfo, GlobalRef, ParamInfo, ReturnPoint, TypeDef
from genut.analysis.parser import ParseResult, parse

_FN_KINDS = ("FUNCTION_DECL", "CXX_METHOD", "FUNCTION_TEMPLATE", "CONSTRUCTOR")
_ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


class _Sources:
    """파일 내용 캐시. libclang 오프셋은 바이트 단위다."""

    def __init__(self) -> None:
        self._cache: dict[str, bytes] = {}

    def data(self, path: str) -> bytes:
        if path not in self._cache:
            try:
                self._cache[path] = Path(path).read_bytes()
            except OSError:
                self._cache[path] = b""
        return self._cache[path]

    def text(self, cursor, start=None, end=None) -> str:  # noqa: ANN001
        ext = cursor.extent
        f = ext.start.file
        if f is None:
            return " ".join(t.spelling for t in cursor.get_tokens())
        data = self.data(f.name)
        s = ext.start.offset if start is None else start
        e = ext.end.offset if end is None else end
        return data[s:e].decode("utf-8", errors="replace")


def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class FileAnalysis:
    source: Path
    args: list[str]
    parse: ParseResult
    functions: list[FunctionInfo] = field(default_factory=list)
    dropped_flags: list[str] = field(default_factory=list)
    learned_flags: list[str] = field(default_factory=list)

    def select(self, name: str) -> list[FunctionInfo]:
        """키, 이름, Class::method, 완전한 이름 중 하나로 고른다. 오버로드면 모두."""
        return [f for f in self.functions if name in (f.key, f.name, f.display, f.qualified_name)]

    def get(self, name: str) -> FunctionInfo | None:
        hits = self.select(name)
        return hits[0] if hits else None

    @property
    def errors(self) -> list[str]:
        return [d.short() for d in self.parse.errors]


def analysis_args(entry: CompileEntry, kernel: bool, sanitizer: FlagSanitizer | None, resource_dir: str = "") -> tuple[list[str], list[str]]:
    args = entry.clean_args() + builtin_include_args(entry, resource_dir)
    dropped: list[str] = []
    if sanitizer is not None and kernel:
        args, dropped = sanitizer.sanitize(args)
    target = guess_target(entry, kernel) or (None if kernel else compiler_target(entry))
    if target:
        args = [f"--target={target}"] + args
    return args, dropped


def analyze_file(
    source: Path,
    entry: CompileEntry,
    kernel: bool = False,
    sanitizer: FlagSanitizer | None = None,
    max_struct_fields: int = 30,
    resource_dir: str = "",
) -> FileAnalysis:
    """파일을 파싱하고 정의된 함수를 모두 뽑는다. 알 수 없는 플래그가 있으면 빼고 1회 다시 파싱한다."""
    source = source.resolve()
    args, dropped = analysis_args(entry, kernel, sanitizer, resource_dir)
    pr = parse(source, args, detailed=True)
    learned: list[str] = []
    unknown = FlagSanitizer.unknown_from([d.message for d in pr.diagnostics])
    if unknown:
        args = [a for a in args if a not in unknown]
        dropped += unknown
        learned = unknown
        if sanitizer is not None:
            sanitizer.learn(unknown)
        pr = parse(source, args, detailed=True)
    fa = FileAnalysis(source=source, args=args, parse=pr, dropped_flags=dropped, learned_flags=learned)
    fa.functions = _Extractor(pr, source, args, max_struct_fields, kernel).functions()
    return fa


class _Extractor:
    def __init__(self, pr: ParseResult, source: Path, args: list[str], max_fields: int, kernel: bool = False) -> None:
        import clang.cindex as ci

        self.kernel = kernel
        self.ci = ci
        self.K = ci.CursorKind
        self.tu = pr.tu
        self.source = source
        self.src_key = _norm(source)
        self.args = args
        self.max_fields = max_fields
        self.srcs = _Sources()
        self.include_dirs = self._include_dirs(args)
        self._classes: dict[str, ClassInfo] = {}

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _include_dirs(args: list[str]) -> list[tuple[Path, bool]]:
        out: list[tuple[Path, bool]] = []
        i = 0
        while i < len(args):
            a = args[i]
            for flag, system in (("-isystem", True), ("-iquote", False), ("-I", False)):
                if a == flag and i + 1 < len(args):
                    out.append((Path(args[i + 1]), system))
                    i += 1
                    break
                if a.startswith(flag) and len(a) > len(flag):
                    out.append((Path(a[len(flag):]), system))
                    break
            i += 1
        return out

    def _in_main(self, cursor) -> bool:  # noqa: ANN001
        f = cursor.location.file
        return f is not None and _norm(f.name) == self.src_key

    def include_line(self, header: str) -> str | None:
        hp = Path(header)
        if _norm(hp) == self.src_key:
            return None
        best: tuple[str, bool] | None = None
        for d, system in self.include_dirs:
            try:
                rel = hp.resolve().relative_to(d.resolve()).as_posix()
            except (ValueError, OSError):
                continue
            angle = system or (self.kernel and bool(re.search(r"(^|/)(include|include/uapi|include/generated)$", d.as_posix())))
            if best is None or len(rel) < len(best[0]):
                best = (rel, angle)
        if best is None:
            try:
                rel = os.path.relpath(hp, self.source.parent).replace("\\", "/")
            except ValueError:
                rel = hp.as_posix()
            best = (rel, False)
        rel, angle = best
        return f"#include <{rel}>" if angle else f'#include "{rel}"'

    # -------------------------------------------------------------- API
    def functions(self) -> list[FunctionInfo]:
        out = []
        for c in self._walk_decls(self.tu.cursor):
            info = self._function(c)
            if info is not None:
                out.append(info)
        seen: dict[str, int] = {}
        for f in out:  # 실행 안의 식별자: Class_method, 오버로드는 _2, _3 (파일·스위트·상태 이름에 쓴다)
            base = re.sub(r"\W+", "_", f"{f.class_name}_{f.name}" if f.is_method and f.class_name else f.name)
            seen[base] = seen.get(base, 0) + 1
            f.key = base if seen[base] == 1 else f"{base}_{seen[base]}"
        return out

    def _class_info(self, cls) -> ClassInfo:  # noqa: ANN001
        """멤버 함수의 소속 클래스: 공개 생성자, 멤버 변수, 공개 멤버 함수, 기본 생성 가능 여부."""
        K, A = self.K, self.ci.AccessSpecifier
        key = cls.get_usr() or cls.spelling
        if key in self._classes:
            return self._classes[key]
        d = cls.get_definition() or cls

        def access(c) -> str:  # noqa: ANN001
            try:
                return {A.PUBLIC: "public", A.PROTECTED: "protected", A.PRIVATE: "private"}.get(c.access_specifier, "")
            except ValueError:
                return ""

        def params(c) -> str:  # noqa: ANN001
            return ", ".join(f"{a.type.spelling} {a.spelling}".strip() for a in c.get_arguments())

        ctors, fields, methods, has_ctor, default = [], [], [], False, False
        for c in d.get_children():
            if c.kind == K.CONSTRUCTOR:
                has_ctor = True
                if access(c) == "public" and not c.is_deleted_method():
                    ctors.append(f"{c.spelling}({params(c)})")
                    default = default or not list(c.get_arguments()) or c.is_default_constructor()
            elif c.kind == K.FIELD_DECL:
                fields.append(f"{c.type.spelling} {c.spelling} ({access(c)})")
            elif c.kind in (K.CXX_METHOD, K.FUNCTION_TEMPLATE) and access(c) == "public":
                head = "static " if c.is_static_method() else ""
                tail = " const" if c.is_const_method() else ""
                methods.append(f"{head}{c.result_type.spelling} {c.spelling}({params(c)}){tail}")
        try:
            abstract = d.is_abstract_record()
        except AttributeError:
            abstract = False
        parts = []
        p = d
        while p is not None and p.kind != K.TRANSLATION_UNIT:
            if p.kind in (K.NAMESPACE, K.CLASS_DECL, K.STRUCT_DECL, K.CLASS_TEMPLATE):
                parts.append(p.spelling)
            p = p.semantic_parent
        info = ClassInfo(
            name=d.spelling, qualified_name="::".join(reversed(parts)), kind="struct" if d.kind == K.STRUCT_DECL else "class",
            file=d.location.file.name if d.location.file else "", line=d.location.line, constructors=ctors,
            default_constructible=default or not has_ctor, abstract=abstract, template=d.kind == K.CLASS_TEMPLATE,
            fields=fields[: self.max_fields], methods=methods,
        )
        self._classes[key] = info
        return info

    def _walk_decls(self, node):  # noqa: ANN001, ANN202
        K = self.K
        for c in node.get_children():
            if not self._in_main(c):
                continue
            if c.kind.name in _FN_KINDS and c.is_definition():
                yield c
            elif c.kind in (K.NAMESPACE, K.CLASS_DECL, K.STRUCT_DECL, K.LINKAGE_SPEC, K.UNEXPOSED_DECL):
                yield from self._walk_decls(c)

    def _body(self, cursor):  # noqa: ANN001, ANN202
        return next((c for c in cursor.get_children() if c.kind == self.K.COMPOUND_STMT), None)

    def _function(self, cur) -> FunctionInfo | None:  # noqa: ANN001
        ci, K = self.ci, self.K
        body = self._body(cur)
        if body is None:
            return None
        start, end = cur.extent.start.line, cur.extent.end.line
        head = _collapse(self.srcs.text(cur, end=body.extent.start.offset))
        params = [
            ParamInfo(name=a.spelling, type=a.type.spelling, canonical=a.type.get_canonical().spelling, record=self._record_name(a.type))
            for a in cur.get_arguments()
        ]
        ns, cls, cls_cursor = [], "", None
        p = cur.semantic_parent
        while p is not None and p.kind != K.TRANSLATION_UNIT:
            if p.kind in (K.CLASS_DECL, K.STRUCT_DECL, K.CLASS_TEMPLATE) and not cls:
                cls, cls_cursor = p.spelling, p
            elif p.kind == K.NAMESPACE:
                ns.append(p.spelling)
            p = p.semantic_parent
        namespace = "::".join(reversed(ns))
        qualified = "::".join(x for x in (namespace, cls, cur.spelling) if x)
        is_method = cur.kind in (K.CXX_METHOD, K.CONSTRUCTOR) or (cur.kind == K.FUNCTION_TEMPLATE and cls_cursor is not None)
        acc = ""
        if is_method:
            try:
                acc = {ci.AccessSpecifier.PUBLIC: "public", ci.AccessSpecifier.PROTECTED: "protected",
                       ci.AccessSpecifier.PRIVATE: "private"}.get(cur.canonical.access_specifier, "")
            except ValueError:
                acc = ""
        info = FunctionInfo(
            name=cur.spelling,
            qualified_name=qualified,
            signature=head,
            return_type=cur.result_type.spelling,
            params=params,
            file=str(self.source),
            start_line=start,
            end_line=end,
            body_start=body.extent.start.line,
            is_static=cur.storage_class == ci.StorageClass.STATIC and not is_method,
            is_inline=bool(re.search(r"\binline\b", head)),
            is_method=is_method,
            is_static_method=is_method and cur.kind != K.CONSTRUCTOR and cur.is_static_method(),
            is_template=cur.kind == K.FUNCTION_TEMPLATE,
            class_name=cls,
            namespace=namespace,
            access=acc,
            is_const=cur.kind == K.CXX_METHOD and cur.is_const_method(),
            is_constructor=cur.kind == K.CONSTRUCTOR,
            owner=self._class_info(cls_cursor) if cls_cursor is not None else None,
            source=self.srcs.text(cur),
            line_count=end - start + 1,
        )
        stmts = list(body.get_children())
        info.empty_body = not stmts
        info.only_asm = bool(stmts) and all(s.kind in (K.ASM_STMT, K.MS_ASM_STMT) for s in stmts)
        w = _BodyWalker(self, info)
        w.visit(body, [])
        info.branch_count = len([b for b in info.branches if b.kind != "else"])
        macro_headers = self._macros(cur, info)
        self._types(cur, info)
        self._includes(cur, info, macro_headers)
        info.preproc = self._preproc(start)
        return info

    def _record_name(self, t) -> str:  # noqa: ANN001
        K = self.ci.TypeKind
        seen = 0
        while t.kind in (K.POINTER, K.LVALUEREFERENCE, K.RVALUEREFERENCE, K.CONSTANTARRAY, K.INCOMPLETEARRAY) and seen < 5:
            t = t.get_pointee() if t.kind in (K.POINTER, K.LVALUEREFERENCE, K.RVALUEREFERENCE) else t.element_type
            seen += 1
        c = t.get_canonical()
        if c.kind == K.RECORD:
            return c.spelling.replace("const ", "")
        return ""

    def _macros(self, cur, info: FunctionInfo) -> list[str]:  # noqa: ANN001
        """함수 본문이 쓰는 매크로 상수와, 그것을 정의한 헤더 (테스트가 같은 상수로 기대값을 쓰므로 include가 필요하다)."""
        K = self.K
        headers: list[str] = []
        s, e = cur.extent.start.offset, cur.extent.end.offset
        for c in self.tu.cursor.get_children():
            if c.kind != K.MACRO_INSTANTIATION or not self._in_main(c):
                continue
            if not (s <= c.extent.start.offset <= e):
                continue
            name = c.spelling
            if name in info.constants:
                continue
            d = c.referenced
            text = ""
            if d is not None and d.location.file is not None:
                toks = [t.spelling for t in d.get_tokens()]
                text = " ".join(toks[1:])[:200] if toks else ""
            info.constants[name] = text or "(매크로)"
            if d is not None and d.location.file is not None:
                headers.append(d.location.file.name)
        return headers

    def _types(self, cur, info: FunctionInfo) -> None:  # noqa: ANN001
        seen: set[str] = set()
        types = [a.type for a in cur.get_arguments()] + [cur.result_type]
        for t in types:
            self._add_type(t, info, seen, depth=0)

    def _add_type(self, t, info: FunctionInfo, seen: set[str], depth: int) -> None:  # noqa: ANN001
        ci = self.ci
        TK = ci.TypeKind
        K = self.K
        n = 0
        while t.kind in (TK.POINTER, TK.LVALUEREFERENCE, TK.RVALUEREFERENCE, TK.CONSTANTARRAY, TK.INCOMPLETEARRAY, TK.ELABORATED) and n < 6:
            if t.kind == TK.ELABORATED:
                t = t.get_named_type()
            elif t.kind in (TK.CONSTANTARRAY, TK.INCOMPLETEARRAY):
                t = t.element_type
            else:
                t = t.get_pointee()
            n += 1
        decl = t.get_declaration()
        if decl is None or decl.kind == K.NO_DECL_FOUND or decl.location.file is None:
            return
        if decl.kind == K.TYPEDEF_DECL:
            under = decl.underlying_typedef_type
            self._add_type(under, info, seen, depth)
            return
        definition = decl.get_definition() or decl
        if definition.kind not in (K.STRUCT_DECL, K.UNION_DECL, K.ENUM_DECL, K.CLASS_DECL):
            return
        kind = {K.STRUCT_DECL: "struct", K.UNION_DECL: "union", K.ENUM_DECL: "enum", K.CLASS_DECL: "class"}[definition.kind]
        name = t.get_canonical().spelling.replace("const ", "")
        if name in seen:
            return
        seen.add(name)
        td = TypeDef(name=name, kind=kind, text="", file=definition.location.file.name, line=definition.location.line)
        if kind == "enum":
            vals = [f"{c.spelling} = {c.enum_value}" for c in definition.get_children() if c.kind == K.ENUM_CONSTANT_DECL]
            td.fields = vals
            td.text = f"enum {definition.spelling} {{ " + ", ".join(vals) + " }"
            for c in definition.get_children():
                if c.kind == K.ENUM_CONSTANT_DECL:
                    info.constants.setdefault(c.spelling, str(c.enum_value))
        else:
            fields = [c for c in definition.get_children() if c.kind == K.FIELD_DECL]
            td.fields = [f"{f.type.spelling} {f.spelling}" for f in fields[: self.max_fields]]
            more = " /* ... */" if len(fields) > self.max_fields else ""
            td.text = f"{name} {{ " + "; ".join(td.fields) + ("; " if td.fields else "") + more.strip() + "}"
            inits = []
            for f in fields[: self.max_fields]:
                fk = f.type.get_canonical().kind
                if fk == TK.POINTER:
                    inits.append(f".{f.spelling} = NULL")
                elif fk in (TK.RECORD, TK.CONSTANTARRAY):
                    inits.append(f".{f.spelling} = {{0}}")
                else:
                    inits.append(f".{f.spelling} = 0")
            td.example_init = f"{name} x = {{ " + ", ".join(inits) + " };"
            if depth < 1:
                for f in fields[: self.max_fields]:
                    self._add_type(f.type, info, seen, depth + 1)
        info.types.append(td)

    def _includes(self, cur, info: FunctionInfo, extra: list[str] = ()) -> None:  # noqa: ANN001
        lines: list[str] = []
        canon = cur.canonical
        files = []
        if canon is not None and canon.location.file is not None:
            files.append(canon.location.file.name)
        for d in cur.get_children():
            if d.kind == self.K.FUNCTION_DECL:
                pass
        files += [t.file for t in info.types if t.file]
        files += list(extra)
        for f in files:
            line = self.include_line(f)
            if line and line not in lines:
                lines.append(line)
        info.includes = lines

    def _preproc(self, start_line: int) -> list[str]:
        text = self.srcs.data(str(self.source)).decode("utf-8", errors="replace").splitlines()
        stack: list[str] = []
        for line in text[: max(0, start_line - 1)]:
            s = line.strip()
            m = re.match(r"#\s*(if|ifdef|ifndef|elif|else|endif)\b\s*(.*)", s)
            if not m:
                continue
            kw, rest = m.group(1), m.group(2).split("//")[0].split("/*")[0].strip()
            if kw == "if":
                stack.append(rest)
            elif kw == "ifdef":
                stack.append(f"defined({rest})")
            elif kw == "ifndef":
                stack.append(f"!defined({rest})")
            elif kw == "elif" and stack:
                stack[-1] = f"!({stack[-1]}) && ({rest})"
            elif kw == "else" and stack:
                stack[-1] = f"!({stack[-1]})"
            elif kw == "endif" and stack:
                stack.pop()
        return [c for c in stack if not re.fullmatch(r"!defined\(\w*_H_*\w*\)", c)]


class _BodyWalker:
    def __init__(self, ex: _Extractor, info: FunctionInfo) -> None:
        self.ex = ex
        self.K = ex.K
        self.info = info
        self._calls: set[tuple[str, int]] = set()
        self._globals: dict[str, GlobalRef] = {}

    def text(self, c) -> str:  # noqa: ANN001
        return _collapse(self.ex.srcs.text(c))

    def visit(self, node, ctx: list[str], write: str | None = None) -> None:  # noqa: ANN001
        K = self.K
        k = node.kind
        if k == K.IF_STMT:
            self._if(node, ctx)
            return
        if k in (K.FOR_STMT, K.WHILE_STMT, K.DO_STMT, K.CXX_FOR_RANGE_STMT):
            self._loop(node, ctx)
            return
        if k == K.SWITCH_STMT:
            self._switch(node, ctx)
            return
        if k == K.RETURN_STMT:
            kids = list(node.get_children())
            expr = self.text(kids[0]) if kids else ""
            self.info.returns.append(ReturnPoint(line=node.extent.start.line, expr=expr, condition=ctx[-1] if ctx else ""))
        elif k == K.CALL_EXPR:
            self._call(node)
        elif k == K.DECL_REF_EXPR:
            self._ref(node, write)
        if k in (K.BINARY_OPERATOR, K.COMPOUND_ASSIGNMENT_OPERATOR):
            kids = list(node.get_children())
            op = self._binop(node, kids)
            if len(kids) == 2 and (k == K.COMPOUND_ASSIGNMENT_OPERATOR or op in _ASSIGN_OPS):
                self.visit(kids[0], ctx, "write")
                self.visit(kids[1], ctx, None)
                return
        if k == K.UNARY_OPERATOR:
            toks = [t.spelling for t in node.get_tokens()]
            if toks and (toks[0] in ("++", "--") or toks[-1] in ("++", "--")):
                write = "write"
            elif toks and toks[0] == "&":
                write = "rw"
        for c in node.get_children():
            self.visit(c, ctx, write)

    def _binop(self, node, kids) -> str:  # noqa: ANN001
        if len(kids) != 2:
            return ""
        lhs_end = kids[0].extent.end.offset
        for t in node.get_tokens():
            if t.extent.start.offset >= lhs_end:
                return t.spelling
        return ""

    def _branch(self, kind: str, line: int, cond: str, body) -> None:  # noqa: ANN001
        self.info.branches.append(Branch(kind=kind, line=line, condition=cond, start=body.extent.start.line, end=body.extent.end.line))

    def _if(self, node, ctx: list[str]) -> None:  # noqa: ANN001
        kids = list(node.get_children())
        if len(kids) < 2:
            for c in kids:
                self.visit(c, ctx)
            return
        # 자식 순서: [초기화문·조건 변수 선언?] 조건 then [else]
        K = self.K
        idx = 0
        while idx < len(kids) - 2 and kids[idx].kind in (K.DECL_STMT, K.VAR_DECL):
            idx += 1
        cond = kids[idx]
        then = kids[idx + 1] if idx + 1 < len(kids) else None
        els = kids[idx + 2] if idx + 2 < len(kids) else None
        ctext = self.text(cond)
        self.visit(cond, ctx)
        if then is not None:
            label = f"({ctext}) 참"
            self._branch("if", node.extent.start.line, label, then)
            self.visit(then, ctx + [label])
        if els is not None:
            label = f"({ctext}) 거짓"
            self._branch("else", node.extent.start.line, label, els)
            self.visit(els, ctx + [label])

    def _loop(self, node, ctx: list[str]) -> None:  # noqa: ANN001
        kids = list(node.get_children())
        body = next((c for c in reversed(kids) if c.kind == self.K.COMPOUND_STMT), kids[-1] if kids else None)
        head = self.ex.srcs.text(node, end=body.extent.start.offset) if body is not None else self.text(node)
        m = re.search(r"\((.*)\)\s*$", _collapse(head))
        cond = m.group(1) if m else _collapse(head)
        if node.kind == self.K.DO_STMT:
            tail = self.text(node)
            mm = re.search(r"while\s*\((.*)\)\s*;?\s*$", tail)
            cond = mm.group(1) if mm else cond
        label = f"루프 ({cond}) 진입"
        for c in kids:
            if c is body:
                self._branch("loop", node.extent.start.line, label, body)
                self.visit(c, ctx + [label])
            else:
                self.visit(c, ctx)

    def _switch(self, node, ctx: list[str]) -> None:  # noqa: ANN001
        K = self.K
        kids = list(node.get_children())
        if len(kids) < 2:
            return
        cond, body = kids[0], kids[-1]
        ctext = self.text(cond)
        self.visit(cond, ctx)
        items = list(body.get_children()) if body.kind == K.COMPOUND_STMT else [body]
        labels = [(c, c.extent.start.line) for c in items if c.kind in (K.CASE_STMT, K.DEFAULT_STMT)]
        conds: list[str] = []
        for i, (c, line) in enumerate(labels):
            end = (labels[i + 1][1] - 1) if i + 1 < len(labels) else body.extent.end.line
            if c.kind == K.CASE_STMT:
                val = self.text(next(iter(c.get_children())))
                label = f"switch ({ctext}) == {val}"
                kind = "case"
            else:
                label = f"switch ({ctext}) 기본(default)"
                kind = "default"
            conds.append(label)
            self.info.branches.append(Branch(kind=kind, line=line, condition=label, start=line, end=end))
        for c in items:
            owner = next((i for i in range(len(labels) - 1, -1, -1) if labels[i][1] <= c.extent.start.line), None)
            self.visit(c, ctx + ([conds[owner]] if owner is not None else []))

    def _call(self, node) -> None:  # noqa: ANN001
        K = self.K
        ref = node.referenced
        line = node.extent.start.line
        if ref is None or ref.kind not in (K.FUNCTION_DECL, K.CXX_METHOD, K.CONSTRUCTOR, K.FUNCTION_TEMPLATE):
            name = node.spelling or self.text(node).split("(")[0].strip()
            if name and (name, line) not in self._calls:
                self._calls.add((name, line))
                self.info.calls.append(CallSite(name=name, line=line, indirect=True))
            return
        name = ref.spelling
        if (name, line) in self._calls or any(c.name == name for c in self.info.calls):
            self._calls.add((name, line))
            return
        self._calls.add((name, line))
        d = ref.get_definition()
        canon = ref.canonical
        self.info.calls.append(
            CallSite(
                name=name,
                line=line,
                decl_file=canon.location.file.name if canon is not None and canon.location.file else "",
                decl_line=canon.location.line if canon is not None else 0,
                def_file=d.location.file.name if d is not None and d.location.file else "",
                is_static=ref.storage_class == self.ex.ci.StorageClass.STATIC,
                is_inline=bool(d is not None and re.search(r"\binline\b", " ".join(t.spelling for t in list(d.get_tokens())[:12]))),
                signature=f"{ref.result_type.spelling} {name}({', '.join(a.type.spelling for a in ref.get_arguments())})",
            )
        )

    def _ref(self, node, write: str | None) -> None:  # noqa: ANN001
        K = self.K
        ref = node.referenced
        if ref is None:
            return
        if ref.kind == K.ENUM_CONSTANT_DECL:
            self.info.constants.setdefault(ref.spelling, str(ref.enum_value))
            return
        if ref.kind != K.VAR_DECL:
            return
        parent = ref.semantic_parent
        is_file_scope = parent is not None and parent.kind in (K.TRANSLATION_UNIT, K.NAMESPACE)
        is_static_local = (not is_file_scope) and ref.storage_class == self.ex.ci.StorageClass.STATIC
        if not (is_file_scope or is_static_local):
            return
        name = ref.spelling if is_file_scope else f"(static local) {ref.spelling}"
        g = self._globals.get(name)
        access = write or "read"
        if g is None:
            g = GlobalRef(
                name=name,
                type=ref.type.spelling,
                is_static=ref.storage_class == self.ex.ci.StorageClass.STATIC,
                access=access,
                decl_file=ref.location.file.name if ref.location.file else "",
            )
            self._globals[name] = g
            self.info.globals.append(g)
        elif g.access != access:
            g.access = "rw"
