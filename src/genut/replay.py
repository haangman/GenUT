"""genut replay: 기록된 LLM 응답을 재사용해 다시 실행한다 (설계 11.4).

축약기·분류 규칙·예산을 바꾼 효과를 LLM 비용 없이 확인하는 도구다. 실행 기록의 trace.jsonl에서
llm_call을 차례로 읽어 역할(태그)별 응답 큐를 만든다. --from <단계>를 주면 그 단계와 뒤 단계의 호출은
실제 provider를 부른다(프롬프트를 바꾼 뒤 그 단계만 다시 받아 보기). 수정(Fixer) 호출은 스모크·확장
안에서 일어나도 fix 단계로 기록된다. 기록이 바닥나도 실제 provider로 넘어간다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from genut.context import AppContext
from genut.llm.provider import Capabilities, ChatProvider, Message, ProviderError, Response, Usage
from genut.orchestrator import STAGES, GenOptions, Orchestrator
from genut.state import RunState

_RESPONSE = re.compile(r"^## response\n\n````\n(.*?)\n````\n\n## parsed", re.S | re.M)


def recorded_responses(run_path: Path) -> dict[str, list[tuple[str, str]]]:
    """태그 -> [(단계, 응답 원문)] (기록된 순서)."""
    out: dict[str, list[tuple[str, str]]] = {}
    trace = run_path / "trace.jsonl"
    if not trace.exists():
        return out
    for line in trace.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e.get("type") != "llm_call" or not e.get("prompt_file"):
            continue
        m = _RESPONSE.search((run_path / e["prompt_file"]).read_text(encoding="utf-8"))
        if m:
            out.setdefault(e.get("agent") or e.get("role", ""), []).append((e.get("stage", ""), m.group(1)))
    return out


class ReplayProvider:
    name = "replay"

    def __init__(
        self,
        recorded: dict[str, list[tuple[str, str]]],
        live: ChatProvider | None = None,
        live_from: str = "",
        stage_of: Callable[[], str] = lambda: "",
    ) -> None:
        self.recorded = {k: list(v) for k, v in recorded.items()}
        self.live = live
        self.live_from = live_from
        self.stage_of = stage_of
        self.model = live.model if live else "replay"
        self.capabilities = live.capabilities if live else Capabilities(native_tools=False, json_schema=True, max_context=1_000_000)
        self.served = 0

    def _is_live_stage(self) -> bool:
        stage = self.stage_of()
        return bool(self.live_from) and stage in STAGES and STAGES.index(stage) >= STAGES.index(self.live_from)

    def chat(self, messages: list[Message], **kw: object) -> Response:
        tag = str(kw.get("tag", ""))
        if self.live is not None and self._is_live_stage():
            return self.live.chat(messages, **kw)  # type: ignore[arg-type]
        queue = self.recorded.get(tag)
        if queue:
            _, text = queue.pop(0)
            self.served += 1
            return Response(text=text, usage=Usage(0, 0), model="replay")
        if self.live is not None:
            return self.live.chat(messages, **kw)  # type: ignore[arg-type]
        raise ProviderError(f"기록된 응답이 더 없습니다: {tag}. --from으로 실제 provider를 쓰세요.")

    def count_tokens(self, messages: list[Message]) -> int | None:
        return None


def replay_run(
    ctx: AppContext,
    run_path: Path,
    live_from: str = "",
    live: ChatProvider | None = None,
    backend=None,  # noqa: ANN001 - 테스트에서 가짜 백엔드를 넣는다
    echo: Callable[[str], None] | None = None,
) -> Orchestrator:
    if live_from and live_from not in STAGES:
        raise ValueError(f"--from은 {', '.join(STAGES)} 중 하나여야 합니다: {live_from}")
    state = RunState.load(run_path / "state.json")
    if live_from and live is None:
        from genut.llm.factory import make_provider

        live = make_provider(ctx.config)
    orch = Orchestrator(ctx, Path(state.source_file), GenOptions(functions=list(state.functions), notes=False), backend=backend, echo=echo,
                        label=f"replay-{state.run_id}")
    orch.provider = ReplayProvider(recorded_responses(run_path), live, live_from, stage_of=lambda: getattr(getattr(orch, "env", None), "stage", ""))
    orch.meta = {"replay_of": state.run_id, "replay_live_from": live_from}
    orch.run()
    return orch
