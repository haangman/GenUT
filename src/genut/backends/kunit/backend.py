"""KUnit 백엔드 (설계 6.4, 7.7, 7.8, 8장, 12장 스크립트 계약).

테스트 파일은 함수마다 하나(test_dir), 그것들을 #include 하는 집합 파일(test_file)은 사용자가 Makefile/Kconfig에
한 번 등록해 둔다. 빌드·실행은 사용자 스크립트로 하며 QEMU 1회가 수 분이라 묶어서 부른다.
- preflight: --mode build (케이스 없는 뼈대)
- build: --mode all 한 번으로 빌드와 실행을 하고 실행 결과는 run()이 꺼내 쓴다
- run(only=...): 격리. --filter를 지원하면 --mode run만, 아니면 용의자만 담은 파일로 --mode all
- 결과는 KTAP에서, 크래시는 oops/KASAN 마커로 찾아 책임 프레임과 가설을 붙인다. 축약 로그를 남긴다
- 커버리지는 마지막 실행의 --coverage-out(lcov)을 쓴다. 부팅이 비싸서 통과 케이스만 다시 돌리지 않는다
- 작업 중에는 커널 트리의 등록된 경로에 쓸 수밖에 없다. finish()가 설치하지 않은 파일을 원래대로 되돌린다
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from genut.analysis.compile_db import CompileDB, CompileEntry, load_kernel_cmd
from genut.analysis.extractor import analysis_args
from genut.analysis.flag_sanitizer import FlagSanitizer
from genut.analysis.linkscope import KernelLinkContext
from genut.backends.base import BuildResult, CommandResult, PreflightResult, Rendered, Runner, RunResult, TestUnit, run_command
from genut.backends.kunit import skeleton
from genut.backends.kunit.kbuild import effective_kind, registration_of
from genut.backends.kunit.script import KUnitScript, ScriptResult
from genut.config import GenutConfig
from genut.coverage import FileCoverage, find_file, parse_lcov
from genut.models import CaseResult, CrashBrief
from genut.state import text_hash
from genut.verify.diagnostics import attribute, parse_compiler_output
from genut.verify.logs.crash import CrashContext, build_brief, find_marker, make_source_reader
from genut.verify.logs.ktap import parse_ktap, strip_timestamps, suite_summary
from genut.verify.logs.reduce import reduce_kunit_log
from genut.verify.staged import CheckResult, semantic_check, werror_enabled
from genut.verify.symbolize import faddr2line

_RESULT_LINE = re.compile(r"^\s*(?:not )?ok \d+\s+-?\s*(\w+)")


def _resolve(base: Path, p: str) -> Path:
    q = Path(p).expanduser()
    return (q if q.is_absolute() else base / q).resolve()


class KernelCompileDB(CompileDB):
    """compile_commands.json이 없거나 항목이 없으면 커널 빌드가 남긴 .<name>.o.cmd를 읽는다 (설계 6.2)."""

    kernel_src: Path | None = None

    def find(self, file: Path) -> CompileEntry | None:
        hit = super().find(file)
        if hit is None and self.kernel_src is not None:
            hit = load_kernel_cmd(file, self.kernel_src)
        return hit


class KUnitBackend:
    name = "kunit"

    def __init__(self, cfg: GenutConfig, product_root: Path, runner: Runner = run_command) -> None:
        k = cfg.kunit
        self.cfg = cfg
        self.kernel_src = _resolve(product_root, k.kernel_src) if k.kernel_src else product_root.resolve()
        self.test_file = _resolve(self.kernel_src, k.test_file) if k.test_file else self.kernel_src / "genut_kunit.c"
        self.test_dir = _resolve(self.kernel_src, k.test_dir) if k.test_dir else self.test_file.parent
        script = _resolve(self.kernel_src, k.run_script) if k.run_script else Path("")
        self.script = KUnitScript(script, self.test_file, k.script_timeout_sec, runner, cwd=self.kernel_src)
        self.runner = runner
        self._originals: dict[Path, str | None] = {}  # 작업 중 쓴 파일의 원래 내용 (없던 파일은 None)
        self._installed: set[Path] = set()
        self._last: ScriptResult | None = None  # 마지막 --mode all 결과. run()이 꺼내 쓴다
        self._units: dict[str, TestUnit] = {}

    # ------------------------------------------------------------ 이름·경로
    def file_name(self, unit: TestUnit) -> str:
        return self.cfg.kunit.test_file_pattern.format(src=unit.source.stem, func=unit.function)

    def render(self, unit: TestUnit, cases=None) -> Rendered:  # noqa: ANN001 - list[TestCase] | None
        r = skeleton.render(unit, self.file_name(unit), cases, self.test_dir)
        r.line_map.path = str(self.test_dir / r.file_name)
        return r

    def _dotconfig(self) -> str:
        p = self.kernel_src / ".config"
        return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""

    def _db_path(self) -> Path:
        k = self.cfg.kunit
        return _resolve(self.kernel_src, k.compile_commands) if k.compile_commands else self.kernel_src / "compile_commands.json"

    # ------------------------------------------------------------ 구성·분석
    def configure(self, units: list[TestUnit]) -> CommandResult:
        """커널 빌드 구성은 사용자 스크립트의 몫이다. 여기서는 계약에 필요한 것이 있는지만 본다."""
        if units:
            self._units = {u.function: u for u in units}
        problems = []
        if not self.script.script.is_file():
            problems.append(f"실행 스크립트가 없습니다: {self.script.script} ([kunit] run_script)")
        if not self.cfg.kunit.test_file:
            problems.append("[kunit] test_file(사용자가 Makefile/Kconfig에 등록한 집합 파일)이 비어 있습니다")
        return CommandResult(["kunit-configure"], 1 if problems else 0, "\n".join(problems), 0.0)

    def compile_db(self) -> CompileDB:
        p = self._db_path()
        db = KernelCompileDB.load(p) if p.exists() else KernelCompileDB(path=None, entries=[])
        db.kernel_src = self.kernel_src
        return db

    def link_context(self, unit: TestUnit) -> KernelLinkContext:
        """테스트가 built-in이면 vmlinux에 들어가는 파일, 모듈이면 EXPORT_SYMBOL만 링크된다 (설계 6.4)."""
        reg = registration_of(self.kernel_src, str(self.test_file))
        kind = effective_kind(reg, self._dotconfig()) if reg else "built-in"
        return KernelLinkContext(kernel_src=self.kernel_src, test_kind="module" if kind == "module" else "built-in",
                                 built_sources=self.compile_db().files())

    def fingerprint(self) -> str:
        """사전검증 캐시 열쇠: .config, compile DB, 실행 스크립트, KUnit 설정이 같으면 뼈대 빌드를 건너뛴다 (설계 7.7)."""
        parts = [json.dumps(self.cfg.kunit.model_dump(), sort_keys=True), self._dotconfig()]
        for p in (self._db_path(), self.script.script):
            parts.append(p.read_text(encoding="utf-8", errors="replace") if p.is_file() else "")
        return text_hash("\n".join(parts))

    # ------------------------------------------------------------ 파일 쓰기
    def _write(self, path: Path, text: str) -> None:
        if path not in self._originals:
            self._originals[path] = path.read_text(encoding="utf-8") if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _write_set(self, rendered: list[Rendered]) -> None:
        """함수별 파일과 그것만 담은 집합 파일을 쓴다 (작업 중에는 이번 실행의 파일만, 설계 7.8)."""
        sources: list[Path] = []
        for r in rendered:
            self._write(Path(r.line_map.path), r.text)
            u = self._units.get(r.function)
            if u is not None and u.report.function.is_static and self.cfg.kunit.static_access == "include_source":
                sources.append(u.source)
        files = [(Path(r.line_map.path), r.suite) for r in rendered]
        self._write(self.test_file, skeleton.aggregate(self.test_file, files, sources))

    # ------------------------------------------------------------ 사전검증
    def preflight(self, units: list[TestUnit], out_dir: Path | None = None) -> PreflightResult:
        empties = [self.render(u, []) for u in units]
        self._units = {u.function: u for u in units}
        self._write_set(empties)
        res = self.script("build", (out_dir or self.kernel_src / ".genut") / "preflight.log")
        if res.built:
            return PreflightResult(True, res.seconds, res.log, res.log_path)
        diags = parse_compiler_output(res.log) if res.category == "build_failed" else []
        hint = {"build_failed": "케이스 없는 뼈대도 빌드되지 않습니다. 집합 파일의 Makefile/Kconfig 등록, 헤더 경로, 스텁 설정을 확인하세요.",
                "timeout": f"스크립트가 {self.cfg.kunit.script_timeout_sec}초 안에 끝나지 않았습니다 (script_timeout_sec).",
                }.get(res.category, f"실행 스크립트가 오류로 끝났습니다 (종료 코드 {res.returncode}).")
        return PreflightResult(False, res.seconds, res.log, res.log_path, diags, "infra_error", hint)

    # ------------------------------------------------------------ 의미 검사
    def check(self, r: Rendered) -> CheckResult:
        """집합 파일 기준으로 의미 검사한다 (설계 7.8). 함수별 파일을 집합 파일 안에 펼쳐 넣고 줄을 되돌려 귀속한다."""
        db = self.compile_db()
        entry = db.find(self.test_file) or db.neighbor(self.test_file)
        if entry is None:
            return CheckResult(False, reason="compile_commands.json에서 집합 파일의 플래그를 찾지 못했습니다.")
        path = Path(r.line_map.path)
        offset = len(skeleton.PRELUDE)  # 집합 파일이 함수별 파일 앞에 넣는 줄
        text = "\n".join(skeleton.PRELUDE) + "\n" + r.text + f"\nkunit_test_suites(&{r.suite});\n"
        args, _ = analysis_args(entry, kernel=True, sanitizer=FlagSanitizer(None), resource_dir=self.cfg.analysis.resource_dir)
        res = semantic_check(path, text, args, werror=werror_enabled(args, self._dotconfig()))
        moved = [d.model_copy(update={"line": d.line - offset}) if Path(d.file).name == path.name and d.line > offset else d for d in res.diagnostics]
        res.diagnostics = attribute(moved, [r.line_map])
        return res

    # ------------------------------------------------------------ 빌드·실행
    def build(self, rendered: list[Rendered], out_dir: Path | None = None) -> BuildResult:
        out = out_dir or self.kernel_src / ".genut"
        self._write_set(rendered)
        res = self.script("all", out / "kunit.log", coverage_out=(out / "coverage") if self.cfg.coverage.enabled else None)
        self._last = res
        if res.built:
            return BuildResult([r.function for r in rendered], [], {r.function: [] for r in rendered}, res.seconds, res.log_path)
        self._last = None
        diags = attribute(parse_compiler_output(res.log), [r.line_map for r in rendered]) if res.category == "build_failed" else []
        by_fn: dict[str, list] = {r.function: [] for r in rendered}
        loose = []
        for d in diags:
            fn = next((r.function for r in rendered if d.owner in r.line_map.cases or Path(d.file).name == r.file_name), None)
            if fn is None:
                loose.append(d.model_copy(update={"owner": d.owner or "@shared"}))
            else:
                by_fn[fn].append(d if d.owner else d.model_copy(update={"owner": "@shared"}))
        if res.category != "build_failed":
            loose.append(_script_diag(res))
        # 한 번역 단위라 하나만 깨져도 모두 실패다. 어느 함수 탓도 아닌 오류는 모든 함수의 공유 영역 몫
        for fn in by_fn:
            if not by_fn[fn]:
                by_fn[fn] = [d.model_copy() for d in loose]
        return BuildResult([], [r.function for r in rendered], by_fn, res.seconds, res.log_path)

    def run(self, r: Rendered, out_dir: Path | None = None, only: list[str] | None = None) -> RunResult:
        if only:
            return self._isolate(r, out_dir, only)
        res = self._last
        if res is None:
            return RunResult(r.function, {c: CaseResult(name=c, status="not_run") for c in r.cases}, -1, 0.0)
        return self._results(r, res, r.cases, out_dir)

    def _isolate(self, r: Rendered, out_dir: Path | None, only: list[str]) -> RunResult:
        """용의자만 다시 실행한다 (설계 8.4). --filter가 있으면 빌드 없이, 없으면 그 케이스만 담은 파일로."""
        out = (out_dir or self.kernel_src / ".genut") / "isolation"
        if self.cfg.kunit.script_supports_filter and self._last is not None:
            res = self.script("run", out / "kunit.log", filter_=f"{r.suite}.{only[0]}")
        else:
            u = self._units.get(r.function)
            alone = self.render(u, [c for c in u.state.cases if c.name in only]) if u else r
            self._write_set([alone])
            res = self.script("all", out / "kunit.log")
            self._last = None  # 다음 빌드에서 원래 파일로 되돌린다
        return self._results(r, res, list(only), out_dir)

    def _results(self, r: Rendered, res: ScriptResult, expected: list[str], out_dir: Path | None) -> RunResult:
        lines = strip_timestamps(res.log)
        suite = parse_ktap(res.log, {r.suite}).get(r.suite)
        results: dict[str, CaseResult] = dict(suite.results) if suite else {}
        crash: CrashBrief | None = None
        hit = find_marker(lines, user=False)
        crashed = ""
        if hit is not None:
            idx = hit[0]
            crashed = next((m.group(1) for line in lines[idx:] if (m := _RESULT_LINE.match(line)) and m.group(1) in expected), "")
            if not crashed:  # 패닉으로 결과 줄이 없다: 결과가 없는 첫 케이스
                crashed = next((c for c in expected if c not in results), "")
            if crashed:
                crash = build_brief(res.log, self._crash_context(r), user=False, suspect_case=crashed)
        timeouts = [c for c, v in results.items() if v.status == "timeout" and c in expected]
        if crash is None and timeouts:
            crash = build_brief("", CrashContext(), timeout_case=timeouts[0])
        if crashed and crash is not None:
            reduced = reduce_kunit_log(lines, suite_summary(suite) if suite else "", "\n\n".join(v.details for v in results.values() if v.details),
                                       crash.render(), hit[0] if hit else None, self.cfg.budget.log_token_budget)
            results[crashed] = CaseResult(name=crashed, status="crash", message=crash.category, details=reduced)
            if out_dir is not None:
                (out_dir / f"{r.function}.run.reduced.md").parent.mkdir(parents=True, exist_ok=True)
                (out_dir / f"{r.function}.run.reduced.md").write_text(reduced, encoding="utf-8")
        elif res.category == "run_failed" and not results:
            crash = CrashBrief(category="unknown", hypothesis="부팅하지 못했습니다. 스크립트 로그를 확인하세요.", raw_excerpt="\n".join(lines[-20:]))
        for c in expected:
            results.setdefault(c, CaseResult(name=c, status="not_run"))
        return RunResult(r.function, {c: results[c] for c in expected}, res.returncode, res.seconds, crash, bool(timeouts), res.log_path)

    def _crash_context(self, r: Rendered) -> CrashContext:
        vmlinux = Path(self.cfg.kunit.vmlinux) if self.cfg.kunit.vmlinux else None
        return CrashContext(
            test_functions=set(r.cases),
            target_functions={r.function},
            our_files=[r.file_name] + ([r.source.name] if r.source else []),
            read_source_line=make_source_reader([self.kernel_src]),
            symbolize=(lambda frame: faddr2line(self.kernel_src, vmlinux, frame)) if vmlinux else None,
        )

    # ------------------------------------------------------------ 커버리지
    def coverage(self, r: Rendered, cases: list[str], out_dir: Path | None = None) -> FileCoverage | None:
        """마지막 실행의 --coverage-out/coverage.info. 실패 케이스가 밟은 줄도 섞인다(부팅을 다시 하지 않으려고)."""
        res = self._last
        if not self.cfg.coverage.enabled or res is None or res.coverage_dir is None or r.source is None:
            return None
        info = res.coverage_dir / "coverage.info"
        if not info.exists():
            return None
        return find_file(parse_lcov(info.read_text(encoding="utf-8", errors="replace")), r.source)

    # ------------------------------------------------------------ 설치·정리
    def install(self, r: Rendered) -> Path:
        """함수별 파일을 확정하고, 집합 파일을 test_dir의 모든 함수별 파일(이전 실행 것 포함)로 다시 쓴다."""
        path = Path(r.line_map.path)
        self._write(path, r.text)
        self._installed.add(path)
        self._write_installed_aggregate()
        return path

    def _write_installed_aggregate(self) -> None:
        pattern = re.compile("^" + re.escape(self.cfg.kunit.test_file_pattern).replace(r"\{src\}", ".+").replace(r"\{func\}", ".+") + "$")
        files = []
        for p in sorted(self.test_dir.iterdir()) if self.test_dir.is_dir() else []:
            if not pattern.match(p.name):
                continue
            if p in self._originals and p not in self._installed:
                text = self._originals[p]  # 이번 실행의 작업 파일: 이전 설치본이 있으면 그것, 없던 파일이면 뺀다
                if text is None:
                    continue
            else:
                text = p.read_text(encoding="utf-8", errors="replace")
            suite = skeleton.suite_of_file(text)
            if suite:
                files.append((p, suite))
        self._write(self.test_file, skeleton.aggregate(self.test_file, files))

    def finish(self) -> None:
        """설치하지 않은 작업 파일을 원래대로 되돌리고, 집합 파일은 설치된 함수별 파일로 맞춘다."""
        for path, original in list(self._originals.items()):
            if path in self._installed or path == self.test_file:
                continue
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(original, encoding="utf-8")
        if self._installed:
            self._write_installed_aggregate()
        elif self.test_file in self._originals:
            original = self._originals[self.test_file]
            if original is None:
                self.test_file.unlink(missing_ok=True)
            else:
                self.test_file.write_text(original, encoding="utf-8")


def _script_diag(res: ScriptResult):  # noqa: ANN202
    from genut.models import Diagnostic

    why = {"timeout": "스크립트 시간 초과", "run_failed": "실행 실패(부팅 불가 등)"}.get(res.category, f"스크립트 오류(종료 코드 {res.returncode})")
    return Diagnostic(message=f"{why}: {res.log_path}", stage="link", owner="@shared")

