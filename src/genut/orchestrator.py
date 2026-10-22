"""오케스트레이터: 생성 실행 하나를 끝까지 진행하는 상태 머신 (설계 3장, 7장).

실행 하나는 소스 파일 하나다. 순서는 코드가 정하고 LLM은 에이전트 호출로 좁은 질문에만 답한다.
  ① 분석 → ② 스텁 → ③ 사전검증 → ④ 스모크 → ⑤ 확장 → ⑥ 수정 → ⑦ 커버리지 → ⑧ 리뷰 → 최종 확인
  (코드 리뷰 검사는 케이스가 통과하는 즉시, Reviewer 에이전트는 ⑧에서 코드가 판단하기 어려운 것만 본다)
- 검증 라운드: 의미 검사로 먼저 걸러 낸 뒤 모든 함수를 한 번에 빌드하고(7.8) 함수별로 실행한다.
  빌드에는 통과·새·미실행 케이스만 넣는다. 실패 케이스는 고친 뒤(new) 다시 들어간다
- 케이스는 단조 진행한다(7.4). 통과 케이스는 바뀌지 않고, 공유 영역이 바뀐 라운드에 통과 케이스가
  깨지면 공유 영역을 되돌린다. 같은 소스로 두 번 결과가 다르면 불안정(flaky)이다(7.10)
- 어떤 종료 경로(예산 소진, give_up, 중단, 예외)에서도 통과 케이스로 최종 확인을 한다(finalize.py)
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from genut.agents.analyzer import Analyzer, UnreachableAnalyzer
from genut.agents.base import AgentEnv, AgentError
from genut.agents.fixer import Fixer, apply_fix, failure_text, give_up
from genut.agents.reviewer import Reviewer
from genut.agents.test_writer import TestWriter
from genut.analysis.extractor import analyze_file
from genut.analysis.flag_sanitizer import FlagSanitizer
from genut.analysis.linkscope import judge_calls
from genut.analysis.model import AnalysisReport
from genut.analysis.summarizer import render_report as render_analysis
from genut.backends.base import Backend, Rendered, RunResult, TestUnit
from genut.backends.stub import NoopStubAdapter, StubAdapter
from genut.budget import Budget
from genut.context import AppContext
from genut.coverage import function_coverage, reach_conditions, render_uncovered
from genut.finalize import final_check
from genut.knowledge import KnowledgeStore
from genut.llm.factory import make_counter, make_provider
from genut.llm.fake_provider import FakeProvider
from genut.llm.provider import ChatProvider
from genut.models import CaseResult, FailureInfo
from genut.report import classify_result, render_report
from genut.rules import load_rules
from genut.state import CaseStatus, CoverageSummary, FunctionState, FunctionStatus, RunState, Snapshot, TestCase, UnreachableLine, text_hash
from genut.trace import RunDir, Trace
from genut.verify.classify import Classifier, allowed_strategies
from genut.verify.review import justified, review_case

STAGES = ("analyze", "stub", "preflight", "smoke", "expand", "fix", "coverage", "review")
STEP = {"analyze": (1, "분석"), "stub": (2, "스텁"), "preflight": (3, "사전검증"), "smoke": (4, "스모크"), "expand": (5, "확장"), "fix": (6, "수정"),
        "coverage": (7, "커버리지"), "review": (8, "리뷰")}
BUILDABLE = (CaseStatus.NEW, CaseStatus.PASS, CaseStatus.NOT_RUN)
FAILING = (CaseStatus.FAIL, CaseStatus.CRASH)
_SUFFIX = re.compile(r"_\d+$")


class StopRun(Exception):
    """실행을 멈춰야 하는 문제 (환경 오류 등). 통과 케이스는 그래도 최종 확인을 거친다."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class BudgetExhausted(Exception):
    pass


class _StopAfter(Exception):
    pass


@dataclass
class GenOptions:
    functions: list[str] = field(default_factory=list)  # 비우면 파일의 모든 비static 함수
    dry_run: bool = False  # LLM·빌드 없이 프롬프트만 만들어 기록 (11.4)
    stop_after: str = ""  # 이 단계 뒤에 멈춤. 최종 확인·설치는 하지 않는다 (genut finalize로 이어 갈 수 있음)
    install: bool = True  # False면 최종 확인까지 하되 프로덕트 트리에 쓰지 않는다 (genut eval)


def make_backend(ctx: AppContext) -> Backend:
    if ctx.config.backend == "gtest":
        from genut.backends.gtest.backend import GTestBackend

        return GTestBackend(ctx.config, ctx.config.product_root())
    from genut.backends.kunit.backend import KUnitBackend

    return KUnitBackend(ctx.config, ctx.config.product_root())


class Orchestrator:
    def __init__(
        self,
        ctx: AppContext,
        source: Path,
        options: GenOptions | None = None,
        *,
        provider: ChatProvider | None = None,
        backend: Backend | None = None,
        stub: StubAdapter | None = None,
        echo: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        label: str = "",
    ) -> None:
        self.ctx = ctx
        self.cfg = ctx.config
        self.source = source.resolve()
        self.opts = options or GenOptions()
        self.provider = provider
        self.backend = backend
        self.stub = stub or NoopStubAdapter()
        self.echo = echo
        self.clock = clock
        self.label = label or (self.source.stem + (f"-{self.opts.functions[0]}" if len(self.opts.functions) == 1 else ""))
        self.units: list[TestUnit] = []
        self.stub_usage: dict[str, str] = {}
        self.shared_owner: dict[str, str] = {}  # 함수 -> 공유 영역을 바꾼 케이스
        self.isolation_used = 0
        self.stopped_after = ""
        self.error = ""
        self.classifier = Classifier()
        self.meta: dict[str, object] = {}  # run.json에 덧붙일 것 (replay 출처 등)

    # ============================================================== 실행
    def run(self) -> RunState:
        self._prepare()
        try:
            for stage in STAGES:
                self._stage(stage)
        except _StopAfter:
            self.stopped_after = self.opts.stop_after
        except BudgetExhausted as e:
            self._note(f"예산 소진으로 멈췄습니다: {e}")
        except StopRun as e:
            self.error = e.category
            self._note(f"{e.category}: {e}")
            self.trace.event("stop", category=e.category, message=str(e))
        except KeyboardInterrupt:
            self.state.interrupted = True
            self._note("사용자가 중단했습니다. 그때까지 통과한 케이스로 마무리합니다.")
        except Exception as e:  # noqa: BLE001 - 내부 예외에도 통과 케이스는 살린다 (7.10)
            self.error = "internal_error"
            self._note(f"내부 오류: {e!r}")
            self.trace.event("internal_error", error=repr(e))
        finally:
            self._finish()
        return self.state

    def _prepare(self) -> None:
        cfg, ctx = self.cfg, self.ctx
        self.rundir = RunDir.create(ctx.runs_root, self.label)
        self.trace = Trace(self.rundir, self.echo)
        self.budget = Budget(cfg.budget, self.clock)
        self.state = RunState(run_id=self.rundir.run_id, source_file=str(self.source), backend=cfg.backend, product=ctx.product.name if ctx.product else "")
        self.rules = load_rules(ctx.home, ctx.product.dir if ctx.product else None, cfg.backend)
        self.knowledge = KnowledgeStore(ctx.home, ctx.product.dir if ctx.product else None, self.state.product)
        if self.provider is None:
            self.provider = FakeProvider() if self.opts.dry_run else make_provider(cfg)
        self.env = AgentEnv(self.provider, make_counter(self.provider, cfg, ctx.home), cfg, self.rules, ctx.home, self.trace, self.budget)
        self.analyzer, self.writer, self.fixer = Analyzer(self.env), TestWriter(self.env), Fixer(self.env)
        self.unreachable = UnreachableAnalyzer(self.env)
        self.reviewer = Reviewer(self.env)
        self.started = datetime.now()
        self._write_run_json()

    def _stage(self, name: str) -> None:
        self.state.stage = self.env.stage = name
        with self.trace.stage(name):
            getattr(self, f"_{name}")()
        self._save()
        if self.opts.stop_after == name:
            raise _StopAfter

    def _say(self, stage: str, text: str) -> None:
        n, title = STEP[stage]
        self.trace.say(f"[{n}/8] {title:<6} {text}")

    def _note(self, text: str) -> None:
        self.state.notes.append(text)

    def _active(self) -> list[TestUnit]:
        return [u for u in self.units if u.state.status == FunctionStatus.ACTIVE]

    # ============================================================= ① 분석
    def _analyze(self) -> None:
        if self.backend is None:
            self.backend = make_backend(self.ctx)
        conf = self.backend.configure([])
        if conf.returncode != 0:
            self.rundir.write_text("configure.log", conf.output)
            raise StopRun("infra_error", "빌드 구성에 실패했습니다. configure.log를 확인하세요: " + conf.output.strip()[-400:])
        db = self.backend.compile_db()
        entry = db.find(self.source) if db else None
        if entry is None:
            raise StopRun("infra_error", f"compile_commands.json에서 {self.source}의 컴파일 명령을 찾지 못했습니다. 이 파일이 빌드에 들어 있는지 확인하세요.")
        kernel = self.cfg.backend == "kunit"
        sanitizer = FlagSanitizer(self.ctx.home / "knowledge" / "flag_sanitizer.learned.yaml") if kernel else None
        fa = analyze_file(self.source, entry, kernel=kernel, sanitizer=sanitizer, max_struct_fields=self.cfg.analysis.max_struct_fields,
                          resource_dir=self.cfg.analysis.resource_dir)
        if fa.errors:
            self._note("분석 중 파싱 오류: " + "; ".join(fa.errors[:3]))
        names = self.opts.functions or [f.name for f in fa.functions if not f.is_static]
        missing = [n for n in names if fa.get(n) is None]
        if missing:
            raise StopRun("input_error", f"{self.source.name}에서 함수를 찾지 못했습니다: {', '.join(missing)}")
        for name in names:
            fn = fa.get(name)
            report = AnalysisReport(function=fn, compile_args=fa.args, parse_errors=fa.errors, dropped_flags=fa.dropped_flags)
            fs = FunctionState(name=fn.name, signature=fn.signature, start_line=fn.start_line, end_line=fn.end_line)
            self.units.append(TestUnit(source=self.source, report=report, state=fs))
            self.state.functions[fn.name] = fs
        self.backend.configure(self.units)  # gtest: link_targets가 비면 테스트별 링크 타깃을 찾는다
        scopes = {}
        for u in self.units:
            fn = u.report.function
            u.report.linkscope = judge_calls(fn, self.source, self.cfg.backend, self.rules, self.backend.link_context(u))
            scopes[fn.name] = [j.model_dump(mode="json") for j in u.report.linkscope]
            self.rundir.write_text(f"analysis/{fn.name}.md", render_analysis(u.report))
            self.rundir.write_text(f"analysis/{fn.name}.json", u.report.model_dump_json(indent=1))
            calls = len(u.report.linkscope)
            stubs = u.report.stub_required()
            self._say("analyze", f"{fn.signature} · 호출 {calls}개 · 스텁 필요 {len(stubs)}개" + (f" ({', '.join(stubs)})" if stubs else ""))
            try:
                verdict = self.analyzer.run(u.report, u.state)
                if verdict.warning:
                    self._note(f"{fn.name}: {verdict.warning}")
                if not verdict.testable:
                    self._say("analyze", f"{fn.name}: 테스트 불가 — {verdict.reason}")
            except AgentError as e:
                self._agent_error(e, u, "분석")
        self.rundir.write_json("linkscope.json", scopes)

    # ============================================================= ② 스텁
    def _stub(self) -> None:
        for u in self._active():
            fn, fs = u.report.function, u.state
            plan = self.stub.plan_stubs(fn, u.report.linkscope, self.rules.must_stub)
            u.stub = self.stub.generate(plan, self.rundir.path / "stubs")
            self.state.stub_symbols += [s.name for s in plan.symbols]
            self.state.stub_includes += [i for i in u.stub.includes if i not in self.state.stub_includes]
            self.state.stub_files += u.stub.files
            usage = self.stub.describe()
            if plan.unsupported:
                usage += "\n이번에 스텁할 수 없는 심볼: " + ", ".join(plan.unsupported)
                fs.narrowed += [f"{s}: 스텁 도구 연동 필요 — 이 심볼을 거치는 경로는 다루지 않음" for s in plan.unsupported]
            self.stub_usage[fs.name] = usage
            include_source = self.cfg.backend == "kunit" and self.cfg.kunit.static_access == "include_source"
            if fn.is_static and self.stub.name == "noop" and not include_source:
                fs.status = FunctionStatus.UNTESTABLE
                fs.status_reason = "static 함수: 테스트에서 부르려면 스텁 도구의 static 접근이 필요합니다 (스텁 도구 연동 필요)"
            stubbed = [s.name for s in plan.symbols]
            self._say("stub", f"{fs.name}: 스텁 {len(stubbed)}개" + (f" ({', '.join(stubbed)})" if stubbed else "") + (f" · 스텁 불가 {', '.join(plan.unsupported)}" if plan.unsupported else ""))

    # ========================================================= ③ 사전검증
    def _preflight(self) -> None:
        units = self._active()
        if not units or self.opts.dry_run:
            return
        cache_path = self.ctx.product.dir / "knowledge" / "preflight.cache.json" if self.ctx.product else None
        key = text_hash(self.backend.fingerprint() + "".join(self.backend.render(u, []).text for u in units))
        cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path and cache_path.exists() else {}
        if key in cache:
            seconds = float(cache[key])
            self._say("preflight", f"캐시 사용 (뼈대·환경이 이전 성공과 같음, {seconds:.1f}s)")
        else:
            self._use_build()
            pf = self.backend.preflight(units, self.rundir.path / "preflight")
            if not pf.ok:
                if pf.category == "header_not_cpp_clean":
                    for u in units:
                        u.state.status = FunctionStatus.UNTESTABLE
                        u.state.status_reason = pf.hint
                raise StopRun(pf.category or "infra_error", f"{pf.hint} (로그: preflight/preflight.log)")
            seconds = pf.seconds
            if cache_path:
                cache[key] = round(seconds, 2)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
            self._say("preflight", f"뼈대 빌드 OK ({seconds:.1f}s)")
        self.state.preflight_seconds = seconds
        if seconds > self.cfg.budget.slow_build_threshold_sec:
            self.state.smoke_skipped = True

    # ============================================================= ④ 스모크
    def _smoke(self) -> None:
        if self.state.smoke_skipped:
            self._say("smoke", f"생략 (사전검증 {self.state.preflight_seconds:.0f}s > {self.cfg.budget.slow_build_threshold_sec}s, 7.7)")
            return
        added = {u.function: self._write(u, "smoke") for u in self._active()}
        self._verify("smoke")
        self._fix_loop()
        self._dedupe(added)
        for u in self._active():
            if not u.state.passed() and not self.opts.dry_run:
                give_up(u.state, self.state.round, "스모크 케이스조차 통과하지 못했습니다")
                self._say("smoke", f"{u.function}: 포기 (give_up)")
        self._summary("smoke")

    # ============================================================= ⑤ 확장
    def _expand(self) -> None:
        added = {u.function: self._write(u, "expand") for u in self._active()}
        self._verify("expand")
        self._fix_loop()
        self._dedupe(added)
        self._summary("expand")

    # ============================================================= ⑥ 수정
    def _fix(self) -> None:
        self._fix_loop()
        self._summary("fix")

    # ========================================================== ⑦ 커버리지
    def _coverage(self) -> None:
        """미커버 줄 → 도달 조건 → 케이스 추가를 목표에 닿거나 라운드가 끝날 때까지 (설계 7.6)."""
        cov = self.cfg.coverage
        if not cov.enabled or self.opts.dry_run:
            self._say("coverage", "꺼짐" if not cov.enabled else "dry-run이라 측정하지 않음")
            return
        rounds = self.cfg.budget.max_coverage_rounds
        for rnd in range(rounds + 1):
            todo = []
            for u in self.units:
                s = self._measure(u, f"coverage/m{rnd}")
                if s is None:
                    continue
                self._say("coverage", f"{u.function}: 라인 {s.percent:.0f}% ({s.covered}/{s.total})" + (" · 미커버 " + " ".join(f":{n}" for n in s.uncovered_lines) if s.uncovered_lines else ""))
                if u.state.status == FunctionStatus.ACTIVE and s.uncovered_lines and s.percent + 1e-9 < cov.target_line_percent:
                    todo.append(u)
            if not todo or rnd == rounds:
                break
            if not self.budget.can_build():
                self._note("빌드 예산이 없어 커버리지 라운드를 멈췄습니다")
                break
            self.budget.coverage_rounds += 1
            for u in todo:
                lines = u.state.coverage.uncovered_lines
                self._write(u, "coverage", uncovered=render_uncovered(reach_conditions(u.report, lines)), covers=lines)
            self._verify("coverage")
            self._fix_loop()
        for u in self.units:
            s = u.state.coverage
            if s is not None and s.uncovered_lines and s.percent + 1e-9 < cov.target_line_percent:
                self._classify_unreachable(u)

    def _measure(self, u: TestUnit, sub: str) -> CoverageSummary | None:
        """통과 케이스만으로 대상 함수의 라인 커버리지를 잰다. 빌드는 하지 않는다."""
        passed = u.state.passed()
        if not passed:
            return None
        fc = self.backend.coverage(self.backend.render(u, passed), [c.name for c in passed], self.rundir.path / sub)
        s = function_coverage(fc, u.report.function)
        if s is not None:
            u.state.coverage = s
            self.trace.event("coverage", function=u.function, covered=s.covered, total=s.total, uncovered=s.uncovered_lines)
        return s

    def _classify_unreachable(self, u: TestUnit) -> None:
        lines = u.state.coverage.uncovered_lines
        try:
            u.state.unreachable = self.unreachable.classify(u.report, u.state, render_uncovered(reach_conditions(u.report, lines)), lines,
                                                            self.stub_usage.get(u.function, ""))
        except AgentError as e:
            self._agent_error(e, u, "도달 불가 분류")
            u.state.unreachable = [UnreachableLine(line=n, category="unknown", reason=f"분류하지 못함 ({e.kind})") for n in lines]
        self._say("coverage", f"{u.function}: 남은 미커버 " + ", ".join(f":{x.line} {x.category}" for x in u.state.unreachable))

    # ============================================================= ⑧ 리뷰
    def _review(self) -> None:
        """코드 검사를 통과한 케이스를 Reviewer가 본다. 지적된 케이스는 1회 고치거나 제거한다 (설계 3.2 ⑧).

        수정은 빌드가 한 번 더 들므로 빌드 예산이 있을 때만 한다. 고친 케이스는 다시 검증·코드 검사를 받고
        Reviewer에게 다시 가지 않는다. 제거로 커버리지가 떨어지면 최종 확인 뒤 다시 잰 값이 리포트에 남는다.
        """
        flagged = False
        for u in self.units:
            if not u.state.passed():
                continue
            try:
                res = self.reviewer.review(u.report, u.state)
            except AgentError as e:
                self._agent_error(e, u, "리뷰")
                continue
            for f in res.findings:
                c = u.state.case(f.case)
                u.state.review_notes.append(f"{c.name}: {f.kind} — {f.detail}")
                if c.review_fixes_used >= 1 or u.state.status != FunctionStatus.ACTIVE or not self.budget.can_build() or self.opts.dry_run:
                    c.drop(self.state.round, "review", f"리뷰 지적({f.kind}): {f.detail}")
                    continue
                c.review_fixes_used += 1
                c.fixes_used = max(c.fixes_used, self.cfg.budget.max_case_fixes - 1)  # 리뷰 지적은 한 번만 고친다
                self._fail(c, FailureInfo(category="review_reject", stage="review", summary=f.detail, review_check=f.kind,
                                          recommended_strategy="fix_case"), "review")
                flagged = True
            self._say("review", f"{u.function}: 지적 {len(res.findings)}건" + (f" ({', '.join(f'{x.case}:{x.kind}' for x in res.findings)})" if res.findings else ""))
        if flagged:
            self._fix_loop()

    def _summary(self, stage: str) -> None:
        for u in self.units:
            c = [x.status for x in u.state.cases]
            self._say(stage, f"{u.function}: 통과 {c.count(CaseStatus.PASS)} · 실패 {c.count(CaseStatus.FAIL) + c.count(CaseStatus.CRASH)} · "
                             f"제거 {sum(1 for x in u.state.cases if not x.is_live)}" + (f" · {u.state.status.value}" if u.state.status != FunctionStatus.ACTIVE else ""))

    # ============================================================ 작성
    def _write(self, u: TestUnit, mode: str, uncovered: str = "", covers: list[int] | None = None) -> list[str]:
        """케이스를 쓴다. candidates개 후보를 반복 호출로 만들고 케이스 단위로 합친다 (4.4)."""
        names: list[str] = []
        for _ in range(max(1, self.cfg.budget.candidates)):
            try:
                out = self.writer.write(mode, u.report, u.state, self.state.round, self.stub_usage.get(u.function, ""), self.knowledge,
                                        uncovered=uncovered, covers=covers)
            except AgentError as e:
                self._agent_error(e, u, f"작성({mode})")
                break
            names += [c.name for c in out.cases]
            for n in out.notes:
                self.trace.event("writer_note", function=u.function, note=n)
        self._say(mode, f"{u.function}: 케이스 {len(names)}개 생성")
        return names

    def _dedupe(self, added: dict[str, list[str]]) -> None:
        """후보를 합친 뒤 같은 목적(같은 이름 바탕)의 케이스가 여럿 통과하면 하나만 남긴다."""
        if self.cfg.budget.candidates <= 1:
            return
        for u in self.units:
            seen: set[str] = set()
            for name in added.get(u.function, []):
                c = u.state.case(name)
                if c is None or c.status != CaseStatus.PASS:
                    continue
                base = "smoke" if c.origin == "smoke" else _SUFFIX.sub("", name)
                if base in seen:
                    c.drop(self.state.round, "merge", "같은 목적의 케이스가 이미 통과했습니다 (후보 합치기)")
                seen.add(base)

    # ============================================================ 검증
    def _use_build(self) -> None:
        if not self.budget.can_build():
            why = "시간" if self.budget.time_exceeded() else "빌드 횟수"
            raise BudgetExhausted(f"{why} 예산 (최종 확인 몫 {self.cfg.budget.final_build_reserve}회는 남겨 둠)")
        self.budget.record_build()
        self.state.builds_used = self.budget.builds_used

    def _buildable(self, u: TestUnit) -> list[TestCase]:
        return [c for c in u.state.cases if c.status in BUILDABLE]

    def _needs_build(self, u: TestUnit) -> bool:
        return any(c.status in (CaseStatus.NEW, CaseStatus.NOT_RUN) or (c.status == CaseStatus.PASS and c.flaky_suspect) for c in u.state.cases) or any(
            c.status == CaseStatus.PASS and c.pass_shared_version != u.state.shared.version for c in u.state.cases
        )

    def _verify(self, stage: str) -> None:
        """한 라운드: 의미 검사 → 묶음 빌드 → 함수별 실행 → 케이스 상태 갱신."""
        if self.opts.dry_run:
            return
        units = [u for u in self._active() if self._needs_build(u)]
        if not units:
            return
        self.state.round += 1
        out = self.rundir.path / "candidates" / f"r{self.state.round}"
        for u in units:
            self._semantic_check(u, stage)
        for _attempt in range(2):  # 케이스 탓인 빌드 실패는 그 케이스를 빼고 한 번 더
            units = [u for u in units if u.state.status == FunctionStatus.ACTIVE and self._buildable(u)]
            if not units:
                break
            rendered = {u.function: self.backend.render(u, self._buildable(u)) for u in units}
            for r in rendered.values():
                self.rundir.write_text(f"candidates/r{self.state.round}/{r.file_name}", r.text)
            self._use_build()
            b = self.backend.build(list(rendered.values()), out)
            retry = []
            for u in units:
                r = rendered[u.function]
                if u.function in b.ok_functions:
                    self._run(u, r, out, stage)
                elif self._build_failed(u, r, b.diagnostics.get(u.function, []) + b.unattributed, stage):
                    retry.append(u)
            self._save()
            if not retry or not self.budget.can_build() or self.state.smoke_skipped:
                break
            units = retry

    def _semantic_check(self, u: TestUnit, stage: str) -> None:
        r = self.backend.render(u, self._buildable(u))
        chk = self.backend.check(r)
        if not chk.available or not chk.diagnostics:
            return
        by_case: dict[str, list] = {}
        for d in chk.diagnostics:
            if d.severity == "error" and d.owner in r.cases:
                by_case.setdefault(d.owner, []).append(d)
        for name, diags in by_case.items():
            c = u.state.case(name)
            if c is not None and c.status == CaseStatus.NEW:
                self._fail(c, self._diag_failure(diags, "check"), stage)

    def _diag_failure(self, diags: list, stage: str) -> FailureInfo:
        cls = self.classifier.classify_diagnostics(diags)
        return FailureInfo(category=cls.category, stage=stage if stage == "check" else (diags[0].stage if diags else "compile"),
                           summary=diags[0].short() if diags else "", diagnostics=diags[:5], recommended_strategy=cls.strategy)

    def _build_failed(self, u: TestUnit, r: Rendered, diags: list, stage: str) -> bool:
        """빌드 실패를 케이스에 나눈다. 케이스 탓이면 그 케이스를 빼고 다시 빌드할 수 있으므로 True."""
        fs = u.state
        errors = [d for d in diags if d.severity == "error"]
        owned: dict[str, list] = {}
        for d in errors:
            if d.owner in r.cases:
                owned.setdefault(d.owner, []).append(d)
        if owned:
            for name, ds in owned.items():
                c = fs.case(name)
                if c.status == CaseStatus.PASS:
                    if c.pass_shared_version != fs.shared.version:
                        self._rollback(u, [c], "공유 영역 변경 뒤 통과 케이스가 컴파일되지 않습니다")
                        return True
                    continue  # 그대로인 통과 케이스는 원인이 아니다 (진단 귀속이 어긋난 경우)
                self._fail(c, self._diag_failure(ds, "compile"), stage)
            return True
        if any(c.status == CaseStatus.PASS and c.pass_shared_version != fs.shared.version for c in fs.cases):
            self._rollback(u, fs.passed(), "공유 영역 변경 뒤 빌드가 깨졌습니다")
            return True
        shared = self._diag_failure(errors or diags, "compile")
        if not errors and not diags:
            shared.summary = "빌드가 실패했지만 진단을 읽지 못했습니다 (build.log 확인)"
        for c in r.cases:
            case = fs.case(c)
            if case.status in (CaseStatus.NEW, CaseStatus.NOT_RUN):
                self._fail(case, shared.model_copy(deep=True), stage)
        return False

    def _run(self, u: TestUnit, r: Rendered, out: Path, stage: str) -> None:
        rr = self.backend.run(r, out)
        if rr.crash is not None:
            self.rundir.write_text(f"crash/r{self.state.round}-{u.function}.md", rr.crash.render() + "\n\n## 원시 로그 발췌\n\n```\n" + rr.crash.raw_excerpt + "\n```\n")
        self._apply_results(u, r, rr, stage)
        if rr.crash is not None and rr.crash.suspect_case:
            self._isolate(u, r, rr, out)
        fs = u.state
        if r.cases and all(fs.case(n).status == CaseStatus.PASS for n in r.cases):  # 녹색 스냅샷 (7.10)
            rel = f"snapshots/b{self.budget.builds_used}-{r.file_name}"
            self.rundir.write_text(rel, r.text)
            self.state.snapshots.append(Snapshot(build_no=self.budget.builds_used, function=fs.name, path=rel, cases=list(r.cases)))

    def _apply_results(self, u: TestUnit, r: Rendered, rr: RunResult, stage: str) -> None:
        fs = u.state
        regressed: list[TestCase] = []
        for name in r.cases:
            c = fs.case(name)
            res = rr.results.get(name) or CaseResult(name=name, status="not_run")
            was_pass = c.status == CaseStatus.PASS
            if res.status == "pass":
                if was_pass:
                    c.flaky_suspect = False
                    c.pass_shared_version = fs.shared.version
                    continue
                self._passed(u, c, stage)
            elif res.status == "not_run":
                if not was_pass:
                    c.mark(CaseStatus.NOT_RUN, self.state.round, stage, "다른 케이스의 크래시로 실행되지 못함")
            elif was_pass:
                if c.pass_body_hash == c.body_hash() and c.pass_shared_version == fs.shared.version:
                    if c.flaky_suspect:
                        c.drop(self.state.round, stage, "불안정: 소스가 같은데 빌드마다 결과가 달랐습니다", CaseStatus.FLAKY)
                    else:
                        c.flaky_suspect = True  # 다음 빌드에서 다시 확인한다
                else:
                    regressed.append(c)
            else:
                self._fail(c, self._run_failure(res, rr), stage)
        if regressed:
            self._rollback(u, regressed, "공유 영역 변경 뒤 통과 케이스가 실패했습니다")

    def _passed(self, u: TestUnit, c: TestCase, stage: str) -> None:
        """통과 즉시 코드 리뷰 검사(7.9)를 거친다."""
        fs = u.state
        findings = review_case(c, fs.name, fs.shared.helpers, self.rules)
        copied = [f for f in findings if f.check == "copied_actual"]
        others = [f for f in findings if f.check != "copied_actual"]
        if copied and not justified(c.fix_evidence, [p.line for p in u.report.function.returns]):
            c.drop(self.state.round, "review", f"기대값을 직전 실패의 실제값으로 바꿔 통과시켰고 소스 근거가 없습니다 ({copied[0].detail}). 소스 버그일 수 있습니다",
                   CaseStatus.SUSPECTED_BUG)
            self.knowledge.add_suspected_bug(fs.name, c.name, c.drop_reason, text_hash(u.report.function.source), self.state.run_id)
            return
        if others:
            f = others[0]
            self._fail(c, FailureInfo(category="review_reject", stage="review", summary=f.detail, review_check=f.check, recommended_strategy="fix_case"), stage)
            return
        if c.previous_body and c.last_failure is not None:  # 성공한 수정은 다음 실행의 예시가 된다 (11.5)
            self.knowledge.add_fix_example(c.last_failure.category, c.last_failure.summary, c.previous_body, c.body, self.state.run_id, self._identifiers(u))
        c.pass_body_hash = c.body_hash()
        c.pass_shared_version = fs.shared.version
        c.flaky_suspect = False
        c.mark(CaseStatus.PASS, self.state.round, stage)

    def _fail(self, c: TestCase, fi: FailureInfo, stage: str) -> None:
        c.last_failure = fi
        c.mark(CaseStatus.CRASH if fi.category == "crash" else CaseStatus.FAIL, self.state.round, stage, fi.summary[:200])

    def _run_failure(self, res: CaseResult, rr: RunResult) -> FailureInfo:
        if res.status == "crash" and rr.crash is not None:
            cls = self.classifier.classify_run("crash")
            where = f" @ {rr.crash.location}" if rr.crash.location else ""
            return FailureInfo(category="crash", stage="run", summary=f"{rr.crash.category}{where}", crash=rr.crash, recommended_strategy=cls.strategy)
        if res.status == "timeout":
            return FailureInfo(category="timeout", stage="run", summary=res.message, recommended_strategy=self.classifier.classify_run("timeout").strategy)
        cls = self.classifier.classify_run("assertion_failed")
        return FailureInfo(category="assertion_failed", stage="run", summary=res.message, details=res.details, expected=res.expected, actual=res.actual,
                           observed=res.observed, recommended_strategy=cls.strategy)

    def _isolate(self, u: TestUnit, r: Rendered, rr: RunResult, out: Path) -> None:
        """크래시 용의자만 다시 실행해 확정한다 (8.4). 빌드는 다시 하지 않는다."""
        suspect = rr.crash.suspect_case
        c = u.state.case(suspect)
        if c is None or c.last_failure is None or self.isolation_used >= self.cfg.budget.max_isolation_runs:
            return
        self.isolation_used += 1
        iso = self.backend.run(r, out, only=[suspect])
        res = iso.results.get(suspect)
        again = res is not None and res.status in ("crash", "timeout", "fail")
        c.last_failure.details = (c.last_failure.details + "\n" if c.last_failure.details else "") + (
            "격리 실행에서 재현되었습니다." if again else "격리 실행에서는 재현되지 않았습니다 (다른 케이스와의 순서 의존 의심).")
        self.trace.event("isolation", function=u.function, case=suspect, reproduced=again)

    def _rollback(self, u: TestUnit, regressed: list[TestCase], why: str) -> None:
        """공유 영역 변경을 되돌린다 (7.4). 바꾼 케이스는 공유 영역을 바꾸지 않는 방법으로 다시 고쳐야 한다."""
        fs = u.state
        if not fs.rollback_shared():
            return
        owner = self.shared_owner.pop(fs.name, "")
        for c in fs.cases:
            if c.name == owner:
                c.shared_change_blocked = True
                self._fail(c, FailureInfo(category="review_reject", stage="shared", summary=f"{why}. 공유 영역 변경을 되돌렸습니다. 공유 헬퍼를 바꾸지 않고 고치세요.",
                                          review_check="shared_regression", recommended_strategy="fix_case"), "shared")
            elif c.status in FAILING + (CaseStatus.NOT_RUN,) or (c.status == CaseStatus.PASS and (c.pass_shared_version or 0) > fs.shared.version):
                # 되돌린 판에서만 통과한 케이스도 다시 검증한다 (판 번호는 다음 변경 때 다시 쓰인다)
                c.mark(CaseStatus.NEW, self.state.round, "shared", "공유 영역을 되돌려 다시 검증")
        self._note(f"{fs.name}: {why} → 공유 영역을 되돌렸습니다 ({', '.join(c.name for c in regressed)})")
        self.trace.event("shared_rollback", function=fs.name, owner=owner, regressed=[c.name for c in regressed])

    # ========================================================= 수정 루프
    def _fix_loop(self) -> None:
        while True:
            failing = [(u, c) for u in self._active() for c in u.state.cases if c.status in FAILING]
            if not failing:
                return
            if not self.budget.can_iterate():
                self._drop_all(failing, f"전체 수정 라운드({self.cfg.budget.max_iterations}회)를 다 썼습니다")
                return
            if not self.budget.can_build():
                self._drop_all(failing, "빌드 예산을 다 썼습니다")
                return
            self.budget.iterations += 1
            marks = []
            outer, self.env.stage = self.env.stage, "fix"  # 수정 호출은 어느 단계에서 일어나든 fix로 기록한다 (replay --from fix)
            try:
                for u, c in failing:
                    if u.state.status != FunctionStatus.ACTIVE or c.status not in FAILING:
                        continue
                    marks.append(self._fix_case(u, c))
            finally:
                self.env.stage = outer
            self._say("fix", f"round {self.budget.iterations}: " + " · ".join(m for m in marks if m))
            self._verify("fix")

    def _drop_all(self, failing: list[tuple[TestUnit, TestCase]], why: str) -> None:
        for _, c in failing:
            if c.status in FAILING:
                c.drop(self.state.round, "fix", f"{why}: {c.last_failure.summary if c.last_failure else ''}".strip(": "))

    def _fix_case(self, u: TestUnit, c: TestCase) -> str:
        fs, b = u.state, self.cfg.budget
        fi = c.last_failure or FailureInfo(category="unknown", stage="run")
        if c.fixes_used >= b.max_case_fixes:
            c.drop(self.state.round, "fix", f"케이스당 수정 한도({b.max_case_fixes}회)를 넘었습니다: {fi.summary}")
            return f"drop({c.name}: 수정 한도)"
        if fi.category == "crash" and c.fixes_used >= 1:
            c.drop(self.state.round, "fix", f"크래시가 다시 났습니다: {fi.summary}")
            return f"drop({c.name}: 크래시 재발)"
        recommended = fi.recommended_strategy
        if fi.category == "timeout" and c.fixes_used == 0:
            recommended = "fix_case"  # 시간 초과는 1회만 고쳐 본다 (7.3)
        allowed = allowed_strategies(recommended)
        try:
            out, _ = self.fixer.fix(u.report, fs, c, allowed, self.stub_usage.get(fs.name, ""), self.knowledge)
        except AgentError as e:
            if e.kind == "budget":
                for x in fs.cases:
                    if x.status in FAILING:
                        x.drop(self.state.round, "fix", "함수당 토큰 예산을 다 썼습니다")
                return f"drop({fs.name}: 토큰 예산)"
            if e.kind == "context_overflow":
                c.drop(self.state.round, "fix", "프롬프트가 컨텍스트 한도를 넘었습니다 (규칙 md를 줄이세요)")
                fs.narrowed.append(f"{c.name}: context_overflow — 규칙 md 길이를 줄이세요")
                return f"narrow({c.name})"
            if e.kind == "infra_error":
                raise StopRun("infra_error", f"LLM 호출 실패: {e}") from e
            c.fixes_used += 1
            return f"fail({c.name}: 응답 형식 오류)"
        outcome = apply_fix(out, fs, c, allowed, self.state.round, stub_supported=self.stub.name != "noop")
        if outcome.shared_changed:
            self.shared_owner[fs.name] = c.name
        if c.status == CaseStatus.SUSPECTED_BUG:
            self.knowledge.add_suspected_bug(fs.name, c.name, c.drop_reason, text_hash(u.report.function.source), self.state.run_id)
        self.rundir.write_text(f"fixes/r{self.budget.iterations}-{c.name}.md", self._fix_record(c, fi, recommended, allowed, out, outcome))
        self.trace.event("fix", function=fs.name, case=c.name, category=fi.category, recommended=recommended, chosen=out.strategy,
                         applied=outcome.strategy, retest=outcome.retest, notes=outcome.notes)
        return f"{outcome.strategy}({c.name})" + (" → 재검증" if outcome.retest else "")

    @staticmethod
    def _fix_record(c: TestCase, fi: FailureInfo, recommended: str, allowed: list[str], out, outcome) -> str:  # noqa: ANN001
        return "\n".join([
            f"# {c.name}", "", f"- 분류: {fi.category}", f"- 권장 전략: {recommended} (허용: {', '.join(allowed)})",
            f"- Fixer 선택: {out.strategy} → 적용: {outcome.strategy}", f"- 이유: {out.reason}", f"- 근거: {out.evidence}",
            *[f"- 참고: {n}" for n in outcome.notes], "", "## 실패 정보", "", "```", failure_text(fi, "brief"), "```", "",
            "## 전", "", "```", c.previous_body or "", "```", "", "## 후", "", "```", c.body, "```", "",
        ])

    # ============================================================ 공통
    def _agent_error(self, e: AgentError, u: TestUnit, where: str) -> None:
        self.trace.event("agent_error", function=u.function, where=where, kind=e.kind, message=str(e))
        if e.kind == "infra_error":
            raise StopRun("infra_error", f"LLM 호출 실패({where}): {e}") from e
        if e.kind == "context_overflow":
            u.state.narrowed.append(f"{where}: context_overflow — 규칙 md 길이를 줄이세요")
        self._note(f"{u.function} {where}: {e.kind} — {e}")

    def _identifiers(self, u: TestUnit) -> set[str]:
        fn = u.report.function
        ids = {fn.name, *(c.name for c in fn.calls), *fn.constants, *(t.name.split()[-1] for t in fn.types), *(g.name for g in fn.globals)}
        return {i for i in ids if len(i) > 2}

    def _save(self) -> None:
        self.state.seconds_used = round(self.budget.elapsed(), 3)
        self.state.save(self.rundir.state_json)

    def _write_run_json(self, finished: bool = False) -> None:
        b = self.budget
        data = {
            "run_id": self.state.run_id,
            "source_file": self.state.source_file,
            "backend": self.state.backend,
            "product": self.state.product,
            "functions": [u.function for u in self.units] or self.opts.functions,
            "provider": getattr(self.provider, "name", ""),
            "model": getattr(self.provider, "model", ""),
            "dry_run": self.opts.dry_run,
            "stop_after": self.opts.stop_after,
            "rules_hash": text_hash(self.rules.prompt_block()),
            "prompts": {a.role: a.prompt.digest() for a in (self.analyzer, self.writer, self.fixer, self.unreachable, self.reviewer)},
            "started": self.started.isoformat(timespec="seconds"),
            "finished": datetime.now().isoformat(timespec="seconds") if finished else "",
            "stage": self.state.stage,
            "error": self.error,
            "interrupted": self.state.interrupted,
            "result": {f: fs.result for f, fs in self.state.functions.items()},
            "budget": {"builds_used": b.builds_used, "builds_max": b.cfg.max_builds_per_run, "elapsed_sec": round(b.elapsed(), 1),
                       "tokens": dict(b.tokens), "iterations": b.iterations},
            "config": self.cfg.model_dump(mode="json"),
            **self.meta,
        }
        self.rundir.write_json("run.json", data)

    # ============================================================ 마무리
    def _finish(self) -> None:
        try:
            if self.backend is not None and not self.opts.dry_run and not self.stopped_after:
                self.state.stage = self.env.stage = "final"
                try:
                    final_check(self.backend, self.units, self.state, self.budget, self.rundir, self.cfg.finalize.install_unverified,
                                self.trace, interrupted=self.state.interrupted, install=self.opts.install)
                except KeyboardInterrupt:
                    self.state.interrupted = True
                    final_check(self.backend, self.units, self.state, self.budget, self.rundir, self.cfg.finalize.install_unverified,
                                self.trace, interrupted=True, install=self.opts.install)
                if self.cfg.coverage.enabled and not self.state.interrupted:
                    for u in self.units:  # 최종 확인으로 케이스가 빠졌을 수 있으니 설치본 기준으로 다시 잰다
                        if u.state.final_check in ("ok", "partial"):
                            try:
                                self._measure(u, "final/coverage")
                            except Exception as e:  # noqa: BLE001 - 재측정 실패로 마무리를 멈추지 않는다
                                self._note(f"{u.function}: 최종 커버리지를 재지 못했습니다: {e!r}")
            self._backend_finish()
            for fs in self.state.functions.values():
                fs.result = classify_result(fs, self.cfg.coverage.target_line_percent)
            self.state.finished = not self.stopped_after
            self._save()
            self.rundir.write_text("final/report.md", render_report(self.state, self._budget_summary()))
            self._write_run_json(finished=True)
            for fs in self.state.functions.values():
                self.trace.say(f"[확인] {fs.name}: {fs.result or '-'} · 최종 확인 {fs.final_check or '-'}" + (f" → {fs.installed_path}" if fs.installed_path else ""))
            self.trace.say(f"완료 · 빌드 {self.budget.builds_used} · 토큰 {sum(self.budget.tokens.values())} · {self.budget.elapsed():.0f}s → {self.rundir.path}")
        finally:
            self.trace.close()

    def _backend_finish(self) -> None:
        """백엔드 정리 (KUnit: 설치하지 않은 작업 파일을 커널 트리에서 되돌린다). stop-after면 그대로 둔다."""
        finish = getattr(self.backend, "finish", None)
        if finish is None or self.stopped_after:
            return
        try:
            finish()
        except Exception as e:  # noqa: BLE001
            self._note(f"백엔드 정리 중 오류: {e!r}")

    def _budget_summary(self) -> dict:
        return {"builds_used": self.budget.builds_used, "builds_max": self.cfg.budget.max_builds_per_run,
                "elapsed_sec": round(self.budget.elapsed(), 1), "tokens": dict(self.budget.tokens)}


def finalize_run(ctx: AppContext, run_path: Path, backend: Backend | None = None, echo: Callable[[str], None] | None = None) -> RunState:
    """genut finalize: 중단되었거나 미확인으로 남은 실행의 통과 케이스로 최종 확인만 다시 해서 설치한다 (7.10).

    LLM은 부르지 않는다. 빌드는 최종 확인 예약분(final_build_reserve)만 쓴다. 설정은 지금 것이 아니라 그 실행에
    쓴 것(run.json의 config, --set 덮어쓰기 포함)을 써서 같은 빌드 구성으로 확인한다.
    """
    from dataclasses import replace

    from genut.config import GenutConfig
    from genut.finalize import load_units

    meta_path = run_path / "run.json"
    if meta_path.exists():
        recorded = json.loads(meta_path.read_text(encoding="utf-8")).get("config")
        if recorded:
            ctx = replace(ctx, config=GenutConfig.model_validate(recorded))
    state = RunState.load(run_path / "state.json")
    rundir = RunDir(run_path)
    units = load_units(run_path, state)
    backend = backend or make_backend(ctx)
    conf = backend.configure(units)
    if conf.returncode != 0:
        raise StopRun("infra_error", "빌드 구성에 실패했습니다: " + conf.output.strip()[-400:])
    reserve = ctx.config.budget.final_build_reserve
    budget = Budget(ctx.config.budget.model_copy(update={"max_builds_per_run": reserve}))
    base = state.builds_used
    trace = Trace(rundir, echo)
    try:
        trace.event("finalize_start", reserve=reserve)
        final_check(backend, units, state, budget, rundir, ctx.config.finalize.install_unverified, trace)
        finish = getattr(backend, "finish", None)
        if finish is not None:
            finish()
        state.builds_used = base + budget.builds_used
        for fs in state.functions.values():
            fs.result = classify_result(fs, ctx.config.coverage.target_line_percent)
            trace.say(f"[확인] {fs.name}: {fs.result or '-'} · 최종 확인 {fs.final_check or '-'}" + (f" → {fs.installed_path}" if fs.installed_path else ""))
        state.stage, state.interrupted, state.finished = "final", False, True
        state.save(rundir.state_json)
        rundir.write_text("final/report.md", render_report(state))
    finally:
        trace.close()
    return state
