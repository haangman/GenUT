"""gtest 백엔드 (설계 3.2 ③, 7.2, 7.5, 7.8, 8.3).

함수마다 테스트 파일과 실행 파일 하나. 빌드는 라운드마다 cmake --build 한 번으로 여러 타깃을 묶는다.
- preflight: 케이스 없는 뼈대만으로 구성·빌드. 실패하면 infra_error, C 헤더가 C++로 컴파일되지 않으면 header_not_cpp_clean
- check: 빌드 없이 libclang 의미 검사 (전용 빌드의 compile_commands.json 플래그)
- build: 진단을 함수와 케이스에 귀속한다. 파일 밖(헤더·링크)에서 난 진단은 그 함수의 공유 영역(@shared) 몫
- run: --gtest_output=json. 크래시로 JSON이 없으면 stdout의 마지막 [ RUN ] 케이스가 용의자이고,
  새니타이저 리포트가 없으면 gdb 배치 백트레이스로 크래시 브리프를 만든다. 시작하지 못한 케이스는 not_run
- install: 최종 테스트 파일과 관리 CMakeLists만 프로덕트 트리에 쓴다
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from genut.analysis.compile_db import CompileDB
from genut.analysis.extractor import analysis_args
from genut.analysis.linkscope import GTestLinkContext
from genut.backends.base import (
    BuildResult,
    CommandResult,
    PreflightResult,
    Rendered,
    Runner,
    RunResult,
    TestUnit,
    run_command,
)
from genut.backends.gtest import skeleton
from genut.backends.gtest.cmake_runner import (
    CMake,
    CMakeTarget,
    compile_flags,
    file_glob,
    link_closure,
    managed_cmakelists,
    parse_links,
    render_links,
    suggest_link_targets,
    target_name,
    unit_key,
)
from genut.config import GenutConfig
from genut.models import CaseResult, CrashBrief, Diagnostic
from genut.state import text_hash
from genut.verify.diagnostics import attribute, parse_compiler_output
from genut.verify.logs.crash import HYPOTHESES, CrashContext, build_brief
from genut.verify.logs.gtest_json import parse_gtest_json, parse_gtest_stdout
from genut.verify.staged import CheckResult, semantic_check, werror_enabled

_BLOCK_END = re.compile(r"^(?:\[\d+/\d+\] |ninja: )")
_TARGET = re.compile(r"\b(genut_[A-Za-z0-9_]+)")
# 시그널로 죽었을 때의 종료 코드: POSIX는 음수 시그널 번호나 128+n, Windows는 NTSTATUS
_SEGV_CODES = {-11, 139, 0xC0000005, -1073741819}
_ABORT_CODES = {-6, 134, 3, 0xC0000409, -1073740791}


def _resolve(root: Path, p: str) -> Path:
    q = Path(p).expanduser()
    return (q if q.is_absolute() else root / q).resolve()


def _exit_category(code: int) -> str:
    if code in _SEGV_CODES:
        return "segv"
    if code in _ABORT_CODES:
        return "abort"
    return "unknown"


class GTestBackend:
    name = "gtest"

    def __init__(self, cfg: GenutConfig, product_root: Path, runner: Runner = run_command) -> None:
        g = cfg.gtest
        self.cfg = cfg
        self.root = product_root.resolve()
        self.cmake = CMake(cfg, _resolve(self.root, g.cmake_source_dir), _resolve(self.root, g.cmake_build_dir), runner)
        self.runner = runner
        self.links: dict[str, list[str]] = {}  # 테스트별 링크 타깃 (link_targets가 비었을 때 File API로 찾은 것)
        self._targets: dict[str, CMakeTarget] = {}
        self._configured: set[str] | None = None  # 마지막 구성 때 있던 작업 파일

    @property
    def build_dir(self) -> Path:
        return self.cmake.build_dir

    @property
    def work_dir(self) -> Path:
        return self.cmake.work_dir

    def file_name(self, unit: TestUnit) -> str:
        return self.cfg.gtest.test_file_pattern.format(src=unit.source.stem, func=unit.function)

    def render(self, unit: TestUnit, cases=None) -> Rendered:  # noqa: ANN001 - list[TestCase] | None
        r = skeleton.render(unit, self.file_name(unit), cases)
        r.line_map.path = str(self.work_dir / r.file_name)
        return r

    # ---------------------------------------------------------- configure
    def _work_files(self) -> set[str]:
        if not self.work_dir.is_dir():
            return set()
        return {p.name for p in self.work_dir.glob(file_glob(self.cfg.gtest.test_file_pattern))}

    def links_for(self, file_name: str) -> list[str]:
        return self.links.get(unit_key(file_name)) or list(self.cfg.gtest.link_targets)

    def configure(self, units: list[TestUnit] | tuple[TestUnit, ...] = ()) -> CommandResult:
        """전용 빌드 디렉터리를 구성한다. link_targets가 비어 있으면 File API로 대상 소스가 속한 타깃을 찾아
        그 테스트 파일에만 쓴다 (소스 파일마다 속한 라이브러리가 다르다)."""
        self.cmake.write_work_lists(self.links)
        res = self.cmake.configure()
        if res.returncode == 0:
            self._targets = self.cmake.codemodel()
            if not self.cfg.gtest.link_targets and units:
                found = {unit_key(self.file_name(u)): suggest_link_targets(self._targets, u.source) for u in units}
                found = {k: v for k, v in found.items() if v and self.links.get(k) != v}
                if found:
                    self.links.update(found)
                    self.cmake.write_work_lists(self.links)
                    again = self.cmake.configure()
                    res = CommandResult(again.cmd, again.returncode, res.output + again.output, res.seconds + again.seconds)
                    self._targets = self.cmake.codemodel()
        self._configured = self._work_files() if res.returncode == 0 else None
        return res

    def compile_db(self) -> CompileDB | None:
        p = self.build_dir / "compile_commands.json"
        return CompileDB.load(p) if p.exists() else None

    def fingerprint(self) -> str:
        """사전검증 캐시 열쇠의 환경 부분: 구성 결과(캐시·compile DB)와 GenUT 빌드 설정 (설계 7.7)."""
        parts = [json.dumps(self.cfg.gtest.model_dump(), sort_keys=True), json.dumps(compile_flags(self.cfg)), json.dumps(self.links, sort_keys=True)]
        for name in ("CMakeCache.txt", "compile_commands.json"):
            p = self.build_dir / name
            parts.append(p.read_text(encoding="utf-8", errors="replace") if p.exists() else "")
        return text_hash("\n".join(parts))

    def link_context(self, unit: TestUnit) -> GTestLinkContext:
        """그 테스트가 링크하는 타깃(과 의존 타깃)의 소스로 linkable을 판정한다 (설계 6.4)."""
        sources = [s for t in link_closure(self._targets, self.links_for(self.file_name(unit))) for s in t.sources]
        return GTestLinkContext(product_root=self.root, link_sources=sources)

    # -------------------------------------------------------------- files
    def write(self, rendered: list[Rendered]) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        for r in rendered:
            p = self.work_dir / r.file_name
            if not p.exists() or p.read_text(encoding="utf-8") != r.text:
                p.write_text(r.text, encoding="utf-8")

    def _clear_work(self, keep: set[str]) -> None:
        for name in self._work_files() - keep:
            (self.work_dir / name).unlink(missing_ok=True)

    def _exe(self, target: str) -> Path:
        t = self._targets.get(target)
        if t is not None and t.artifacts:
            exe = next((a for a in t.artifacts if a.suffix.lower() in ("", ".exe")), t.artifacts[0])
            return exe
        return self.build_dir / (target + (".exe" if os.name == "nt" else ""))

    def _log(self, out_dir: Path | None, name: str, text: str) -> Path | None:
        if out_dir is None:
            return None
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / name
        p.write_text(text, encoding="utf-8")
        return p

    # ---------------------------------------------------------- preflight
    def preflight(self, units: list[TestUnit], out_dir: Path | None = None) -> PreflightResult:
        """케이스 없는 뼈대만으로 구성·빌드해 환경 문제를 LLM 호출 전에 걸러 낸다 (설계 3.2 ③)."""
        t0 = time.monotonic()
        empties = [self.render(u, []) for u in units]
        self._clear_work({r.file_name for r in empties})
        self.write(empties)
        conf = self.configure(units)
        if conf.returncode != 0:
            log = self._log(out_dir, "preflight.log", conf.output)
            return PreflightResult(False, time.monotonic() - t0, conf.output, log, category="infra_error",
                                   hint="CMake 구성에 실패했습니다. gtest(find_package)와 [gtest] 설정을 확인하세요.")
        b = self.cmake.build([target_name(r.file_name) for r in empties])
        text = conf.output + b.output
        log = self._log(out_dir, "preflight.log", text)
        if b.returncode == 0:
            return PreflightResult(True, time.monotonic() - t0, text, log)
        diags = parse_compiler_output(b.output)
        ours = {os.path.normcase(os.path.normpath(r.line_map.path)) for r in empties}
        in_headers = [d for d in diags if d.severity == "error" and d.file and os.path.normcase(os.path.normpath(d.file)) not in ours]
        if in_headers and any(u.source.suffix in skeleton.C_SOURCE_EXT for u in units):
            return PreflightResult(False, time.monotonic() - t0, text, log, diags, "header_not_cpp_clean",
                                   "대상 헤더가 C++로 컴파일되지 않습니다(예: C++ 예약어를 식별자로 사용). 사람이 헤더를 확인해야 합니다.")
        return PreflightResult(False, time.monotonic() - t0, text, log, diags, "infra_error",
                               "케이스 없는 뼈대도 빌드되지 않습니다. 헤더 경로·링크 타깃·스텁 설정을 확인하세요.")

    # -------------------------------------------------------------- check
    def check(self, r: Rendered) -> CheckResult:
        """빌드 없이 libclang 의미 검사 (설계 7.2 1단계). 진단의 owner를 케이스로 채운다."""
        path = self.work_dir / r.file_name
        db = self.compile_db()
        entry = (db.find(path) or db.neighbor(path)) if db else None
        if entry is None:
            return CheckResult(False, reason="compile_commands.json에서 테스트 파일의 플래그를 찾지 못했습니다.")
        args, _ = analysis_args(entry, kernel=False, sanitizer=None, resource_dir=self.cfg.analysis.resource_dir)
        res = semantic_check(path, r.text, args, werror=werror_enabled(args))
        res.diagnostics = attribute(res.diagnostics, [r.line_map])
        return res

    # -------------------------------------------------------------- build
    def build(self, rendered: list[Rendered], out_dir: Path | None = None) -> BuildResult:
        t0 = time.monotonic()
        self.write(rendered)
        text = ""
        if self._configured is None or not {r.file_name for r in rendered} <= self._configured:
            conf = self.configure()
            text += conf.output
            if conf.returncode != 0:
                log = self._log(out_dir, "build.log", text)
                return BuildResult([], [r.function for r in rendered], {}, time.monotonic() - t0, log)
        by_target = {target_name(r.file_name): r for r in rendered}
        for t in by_target:
            self._exe(t).unlink(missing_ok=True)  # 이번 빌드의 성공 여부를 실행 파일로 판단한다
        b = self.cmake.build(list(by_target))
        text += b.output
        log = self._log(out_dir, "build.log", text)
        self._targets = self.cmake.codemodel() or self._targets
        ok = [r.function for t, r in by_target.items() if self._exe(t).exists()]
        diags, loose = self._attribute(b.output, by_target)
        return BuildResult(ok, [r.function for r in rendered if r.function not in ok], diags, time.monotonic() - t0, log, loose)

    def _attribute(self, output: str, by_target: dict[str, Rendered]) -> tuple[dict[str, list[Diagnostic]], list[Diagnostic]]:
        """ninja의 FAILED 블록 단위로 진단을 함수에 나누고, 줄 번호로 케이스에 귀속한다."""
        blocks: list[tuple[str, list[str]]] = [("", [])]
        for line in output.splitlines():
            if line.startswith("FAILED: "):
                m = _TARGET.search(line)
                blocks.append((m.group(1) if m and m.group(1) in by_target else "", []))
            elif _BLOCK_END.match(line):
                blocks.append(("", []))
            else:
                blocks[-1][1].append(line)
        maps = [r.line_map for r in by_target.values()]
        out: dict[str, list[Diagnostic]] = {r.function: [] for r in by_target.values()}
        loose: list[Diagnostic] = []
        for target, lines in blocks:
            for d in attribute(parse_compiler_output("\n".join(lines)), maps):
                fn = by_target[target].function if target else self._function_of(d, by_target)
                if fn is None:
                    loose.append(d)
                    continue
                out[fn].append(d if d.owner else d.model_copy(update={"owner": "@shared"}))
        return out, loose

    @staticmethod
    def _function_of(d: Diagnostic, by_target: dict[str, Rendered]) -> str | None:
        for r in by_target.values():
            if d.file and os.path.normcase(os.path.normpath(d.file)) == os.path.normcase(os.path.normpath(r.line_map.path)):
                return r.function
            if d.owner and d.owner in r.line_map.cases:
                return r.function
        return None

    # ---------------------------------------------------------------- run
    def run(self, r: Rendered, out_dir: Path | None = None, only: list[str] | None = None) -> RunResult:
        """함수 하나의 실행 파일을 돌린다. only가 있으면 그 케이스만 (--gtest_filter, 케이스 격리)."""
        exe = self._exe(target_name(r.file_name))
        work = (out_dir or self.build_dir / "genut-run") / f"run-{r.function}"
        work.mkdir(parents=True, exist_ok=True)  # 생성 코드가 만드는 파일은 여기에만 (호스트 보호)
        json_path = work / "result.json"
        json_path.unlink(missing_ok=True)
        cmd = [str(exe), f"--gtest_output=json:{json_path}", "--gtest_color=no"]
        if only:
            cmd.append("--gtest_filter=" + ":".join(f"{r.suite}.{c}" for c in only))
        res = self.runner(cmd, cwd=work, timeout=self.cfg.gtest.test_timeout_sec)
        text = res.output
        expected = only or r.cases
        parsed = None if res.timed_out else parse_gtest_json(json_path)
        crash: CrashBrief | None = None
        if parsed is not None:
            results = parsed
        else:
            results, running = parse_gtest_stdout(text)
            if res.timed_out:
                if running:
                    results[running] = CaseResult(name=running, status="timeout", message=f"{self.cfg.gtest.test_timeout_sec}초 안에 끝나지 않았습니다.")
                crash = build_brief("", CrashContext(), timeout_case=running or "")
            else:
                crash, extra = self._crash(r, exe, work, text, res.returncode, running)
                text += extra
                if running:
                    results[running] = CaseResult(name=running, status="crash", message=crash.category, details=crash.render())
        for c in expected:
            results.setdefault(c, CaseResult(name=c, status="not_run"))
        log = self._log(out_dir, f"{r.function}.run.log", text)
        return RunResult(r.function, results, res.returncode, res.seconds, crash, res.timed_out, log)

    def _crash(self, r: Rendered, exe: Path, work: Path, text: str, code: int, running: str | None) -> tuple[CrashBrief, str]:
        ctx = CrashContext(
            test_functions={f"{r.suite}_{c}_Test::TestBody" for c in r.cases} | {f"{r.suite}_{c}_Test" for c in r.cases},
            target_functions={r.function},
            our_files=[r.file_name] + ([r.source.name] if r.source else []),
        )
        brief = build_brief(text, ctx, user=True, suspect_case=running or "")
        extra = ""
        gdb = shutil.which("gdb") if self.cfg.gtest.crash_backtrace == "gdb" else None
        vague = brief is None or (brief.category in ("segv", "abort", "unknown") and not brief.chain)
        if vague and gdb and running:  # 새니타이저 리포트가 없으면 gdb로 한 번 더 돌려 백트레이스를 얻는다
            cmd = [gdb, "-batch", "-ex", "run", "-ex", "bt", "--args", str(exe), f"--gtest_filter={r.suite}.{running}", "--gtest_color=no"]
            g = self.runner(cmd, cwd=work, timeout=self.cfg.gtest.test_timeout_sec * 2)
            extra = "\n\n===== gdb 백트레이스 =====\n" + g.output
            brief = build_brief(g.output, ctx, user=True, suspect_case=running) or brief
        if brief is None:
            cat = _exit_category(code)
            brief = CrashBrief(category=cat, suspect_case=running or "", hypothesis=HYPOTHESES.get(cat, HYPOTHESES["unknown"]),
                               raw_excerpt=f"종료 코드 {code}")
        return brief, extra

    # ------------------------------------------------------------ install
    def install(self, r: Rendered) -> Path:
        """최종 파일을 프로덕트 트리에 쓴다. 다른 함수의 파일은 건드리지 않는다 (설계 7.8, 결정 4)."""
        g = self.cfg.gtest
        out_dir = _resolve(self.root, g.output_dir)
        lists_dir = _resolve(self.root, g.cmake_lists_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        lists_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / r.file_name
        path.write_text(r.text, encoding="utf-8")
        rel = os.path.relpath(out_dir, lists_dir)
        (lists_dir / "CMakeLists.txt").write_text(managed_cmakelists(g.link_targets, g.test_file_pattern, rel, g.gtest_root), encoding="utf-8")
        key = unit_key(r.file_name)
        if key in self.links:  # 이 테스트만의 링크 타깃. 다른 함수의 항목은 그대로 둔다
            lf = lists_dir / "genut_links.cmake"
            links = parse_links(lf.read_text(encoding="utf-8")) if lf.exists() else {}
            links[key] = self.links[key]
            lf.write_text(render_links(links), encoding="utf-8")
        return path
