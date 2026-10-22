"""커널 gcc 플래그 정제기 (설계 6.2)."""

from __future__ import annotations

import fnmatch
import re
from importlib import resources
from pathlib import Path

import yaml

_UNKNOWN = re.compile(r"unknown argument:? '([^']+)'")


def _load(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


class FlagSanitizer:
    def __init__(self, learned_path: Path | None = None) -> None:
        base = yaml.safe_load((resources.files("genut.analysis") / "flag_sanitizer.yaml").read_text(encoding="utf-8")) or {}
        self.learned_path = learned_path
        learned = _load(learned_path)
        self.remove: list[str] = list(base.get("remove", [])) + list(learned.get("remove", []))
        self.remove_with_value: set[str] = set(base.get("remove_with_value", []))
        self.append: list[str] = list(base.get("append", []))

    def _drop(self, arg: str) -> bool:
        return any(fnmatch.fnmatchcase(arg, pat) for pat in self.remove)

    def sanitize(self, args: list[str]) -> tuple[list[str], list[str]]:
        out, dropped = [], []
        i = 0
        while i < len(args):
            a = args[i]
            if a in self.remove_with_value:
                dropped += args[i : i + 2]
                i += 2
                continue
            if self._drop(a):
                dropped.append(a)
            else:
                out.append(a)
            i += 1
        for a in self.append:
            if a not in out:
                out.append(a)
        return out, dropped

    @staticmethod
    def unknown_from(messages: list[str]) -> list[str]:
        found = []
        for m in messages:
            mm = _UNKNOWN.search(m)
            if mm:
                found.append(mm.group(1))
        return found

    def learn(self, flags: list[str]) -> None:
        """자동으로 뺀 플래그를 학습 목록에 남긴다 (툴체인 특성이라 공통 범위)."""
        new = [f for f in flags if not self._drop(f)]
        if not new:
            return
        self.remove += new
        if self.learned_path is None:
            return
        data = _load(self.learned_path)
        data["remove"] = sorted(set(data.get("remove", [])) | set(new))
        self.learned_path.parent.mkdir(parents=True, exist_ok=True)
        self.learned_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
