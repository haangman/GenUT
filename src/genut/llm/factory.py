"""설정으로 provider와 토큰 카운터를 만든다."""

from __future__ import annotations

from pathlib import Path

from genut.config import GenutConfig
from genut.llm.provider import ChatProvider, Message
from genut.llm.tokens import TokenCounter, load_calibration


def make_provider(cfg: GenutConfig) -> ChatProvider:
    llm = cfg.llm
    if llm.provider == "anthropic":
        from genut.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(llm.anthropic, llm.model, max_retries=llm.max_retries)
    if llm.provider == "openai_compat":
        from genut.llm.openai_compat_provider import OpenAICompatProvider

        return OpenAICompatProvider(llm.openai_compat, max_retries=llm.max_retries)
    from genut.llm.fake_provider import FakeProvider

    if llm.fake.script:
        return FakeProvider.from_file(Path(llm.fake.script), max_context=llm.fake.max_context)
    return FakeProvider(max_context=llm.fake.max_context)


def make_counter(provider: ChatProvider, cfg: GenutConfig, home: Path | None) -> TokenCounter:
    calib = load_calibration(home, provider.model) if home else 1.0
    if cfg.llm.context.token_counter == "api":
        return TokenCounter(calib, exact=lambda text: provider.count_tokens([Message("user", text)]))
    return TokenCounter(calib)
