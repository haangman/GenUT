"""실행 기록 디렉터리와 trace.jsonl (설계 11.2)."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

SUBDIRS = ("prompts", "stubs", "candidates", "fixes", "crash", "snapshots", "final")
SEQ_FILE = ".seq"  # 같은 초에 만든 실행끼리의 순번


def _safe(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_") or "run"


class RunDir:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        for sub in SUBDIRS:
            (path / sub).mkdir(exist_ok=True)

    @classmethod
    def create(cls, runs_root: Path, label: str, now: datetime | None = None) -> RunDir:
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        base = runs_root / f"{stamp}-{_safe(label)}"
        path, i = base, 2
        while path.exists():
            path = base.with_name(f"{base.name}-{i}")
            i += 1
        # 같은 초에 만든 실행끼리의 순서 (이름은 라벨 알파벳순이라 순서를 알 수 없다)
        same = [p for p in runs_root.glob(f"{stamp}-*") if p.is_dir()] if runs_root.exists() else []
        seq = 1 + max((_seq(p) for p in same), default=0)
        rd = cls(path)
        (path / SEQ_FILE).write_text(str(seq), encoding="utf-8")
        return rd

    @property
    def run_id(self) -> str:
        return self.path.name

    @property
    def state_json(self) -> Path:
        return self.path / "state.json"

    @property
    def run_json(self) -> Path:
        return self.path / "run.json"

    @property
    def trace_jsonl(self) -> Path:
        return self.path / "trace.jsonl"

    def sub(self, *parts: str) -> Path:
        p = self.path.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def write_text(self, rel: str, text: str) -> Path:
        p = self.sub(*rel.split("/"))
        p.write_text(text, encoding="utf-8")
        return p

    def write_json(self, rel: str, obj: Any) -> Path:
        return self.write_text(rel, json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _seq(run: Path) -> int:
    try:
        return int((run / SEQ_FILE).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def find_runs(runs_root: Path) -> list[Path]:
    """오래된 순. 이름 앞 15자(초 단위 시각)가 같으면 만든 순번(.seq)으로 가른다 (라벨 알파벳순이 되지 않게)."""
    if not runs_root.exists():
        return []
    return sorted((p for p in runs_root.iterdir() if p.is_dir()), key=lambda p: (p.name[:15], _seq(p), p.name))


def resolve_run(runs_root: Path, ref: str) -> Path:
    """'latest', 'latest~N'(N개 전), 실행 ID(앞부분만 줘도 됨), 경로 중 하나로 실행 디렉터리를 찾는다. latest는 이 프로덕트 안에서."""
    m = re.fullmatch(r"latest(?:~(\d+))?", ref)
    if m:
        runs = find_runs(runs_root)
        back = int(m.group(1) or 0)
        if not runs:
            raise FileNotFoundError(f"실행 기록이 없습니다: {runs_root}")
        if back >= len(runs):
            raise FileNotFoundError(f"{ref}: 실행 기록이 {len(runs)}개뿐입니다.")
        return runs[-1 - back]
    p = Path(ref)
    if p.is_dir():
        return p
    cand = runs_root / ref
    if cand.is_dir():
        return cand
    matches = [r for r in find_runs(runs_root) if r.name.startswith(ref)]
    if len(matches) == 1:
        return matches[0]
    raise FileNotFoundError(f"실행 기록을 찾을 수 없습니다: {ref}")


class Trace:
    """시간순 이벤트 기록. echo가 있으면 진행 메시지를 터미널에도 보낸다."""

    def __init__(self, rundir: RunDir, echo: Callable[[str], None] | None = None) -> None:
        self.rundir = rundir
        self.echo = echo
        self._prompt_no = 0
        self._f = rundir.trace_jsonl.open("a", encoding="utf-8")

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()

    def event(self, type_: str, **data: Any) -> None:
        rec = {"ts": round(time.time(), 3), "type": type_, **data}
        self._f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._f.flush()

    def say(self, message: str) -> None:
        self.event("message", text=message)
        if self.echo:
            try:
                self.echo(message)
            except UnicodeError:  # 콘솔 인코딩 문제로 실행이 멈추면 안 된다 (기록은 이미 남김)
                self.echo(message.encode("ascii", "replace").decode("ascii"))

    @contextmanager
    def stage(self, name: str, **data: Any) -> Iterator[None]:
        t0 = time.monotonic()
        self.event("stage_start", stage=name, **data)
        try:
            yield
        finally:
            self.event("stage_end", stage=name, seconds=round(time.monotonic() - t0, 3))

    def prompt(
        self,
        role: str,
        system: str,
        user: str,
        response: str,
        parsed: Any = None,
        meta: dict[str, Any] | None = None,
    ) -> Path:
        self._prompt_no += 1
        rel = f"prompts/{self._prompt_no:02d}-{_safe(role)}.md"
        parsed_txt = json.dumps(parsed, ensure_ascii=False, indent=2, default=str) if parsed is not None else ""
        body = (
            f"# {role}\n\n"
            f"```json\n{json.dumps(meta or {}, ensure_ascii=False, indent=2, default=str)}\n```\n\n"
            f"## system\n\n````\n{system}\n````\n\n## user\n\n````\n{user}\n````\n\n"
            f"## response\n\n````\n{response}\n````\n\n## parsed\n\n```json\n{parsed_txt}\n```\n"
        )
        path = self.rundir.write_text(rel, body)
        self.event("llm_call", role=role, prompt_file=rel, **(meta or {}))
        return path
