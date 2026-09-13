"""
Provider-agnostic LLM factory for the AI worker.

Project brief, section 3: the LLM API cost is on us, so this project
targets whatever free-tier provider is available, and the bonus list
explicitly names automatic fallback to a second model when the first
one fails.

Design:

1. `LLMProvider` is a closed set of the THREE integration styles this
   worker knows how to build a `BaseChatModel` for. "openai_compatible"
   covers the common case: most free-tier providers (Groq, OpenRouter,
   local Ollama, etc.) expose an OpenAI-shaped `/chat/completions` API,
   so `langchain_openai.ChatOpenAI` pointed at a custom `base_url`
   works for all of them without a provider-specific package each.

2. `build_chat_model` is the ONLY place that imports a provider-specific
   LangChain integration package. The agent graph (graph/agent_graph.py)
   only ever depends on `BaseChatModel` - it does not know or care
   which concrete provider is behind it. Adding a fourth provider later
   means editing this one function, nothing else.

3. `build_model_with_fallback` wires the fallback chain using
   LangChain's own `.with_fallbacks()`, rather than a hand-rolled
   try/except around every call site. `.with_fallbacks()` intercepts
   an exception raised by the primary model's `.invoke()` / `.stream()`
   (auth errors, HTTP timeouts, rate limits, provider outages) and
   transparently retries the SAME call against the next model in the
   list - callers (worker.py) just call `.invoke()`/`.stream()` on the
   object this returns and never see a difference.

4. Configuration is read from environment variables so provider/model
   swaps never require a code change or redeploy - only a `.env` edit
   and a container restart (see .env.example: LLM_PRIMARY_* /
   LLM_FALLBACK_*).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

from langchain_core.language_models import BaseChatModel


class LLMProvider(str, Enum):
    OPENAI_COMPATIBLE = "openai_compatible"
    ANTHROPIC = "anthropic"
    GOOGLE_GENAI = "google_genai"


@dataclass
class LLMProviderConfig:
    provider: LLMProvider
    model: str
    api_key: str
    base_url: str | None = None  # only meaningful for openai_compatible
    temperature: float = 0.2


def build_chat_model(config: LLMProviderConfig) -> BaseChatModel:
    """
    Construct one concrete LangChain chat model from a provider config.
    Imports are done INSIDE each branch (not at module top) so that a
    deployment which only ever configures one provider does not need
    the other two integration packages installed at all.
    """
    if config.provider == LLMProvider.OPENAI_COMPATIBLE:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            temperature=config.temperature,
        )

    if config.provider == LLMProvider.ANTHROPIC:
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=config.model,
            api_key=config.api_key,
            temperature=config.temperature,
        )

    if config.provider == LLMProvider.GOOGLE_GENAI:
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=config.model,
            google_api_key=config.api_key,
            temperature=config.temperature,
        )

    raise ValueError(f"Unknown LLM provider: {config.provider!r}")


def _config_from_env(prefix: str) -> LLMProviderConfig | None:
    """
    Reads one provider config from env vars named `{prefix}_PROVIDER`,
    `{prefix}_MODEL`, `{prefix}_API_KEY`, `{prefix}_BASE_URL` (the last
    only used by openai_compatible). Returns None if `{prefix}_PROVIDER`
    is unset, so an unconfigured fallback slot is simply skipped rather
    than erroring.
    """
    provider_name = os.getenv(f"{prefix}_PROVIDER")
    if not provider_name:
        return None

    return LLMProviderConfig(
        provider=LLMProvider(provider_name),
        model=os.getenv(f"{prefix}_MODEL", ""),
        api_key=os.getenv(f"{prefix}_API_KEY", ""),
        base_url=os.getenv(f"{prefix}_BASE_URL") or None,
        temperature=float(os.getenv(f"{prefix}_TEMPERATURE", "0.2")),
    )


def load_primary_and_fallback_configs() -> list[LLMProviderConfig]:
    """
    Reads LLM_PRIMARY_* (required) and LLM_FALLBACK_* (optional) from
    the environment. Index 0 is always tried first.
    """
    primary = _config_from_env("LLM_PRIMARY")
    if primary is None:
        raise RuntimeError(
            "LLM_PRIMARY_PROVIDER is not set - at least one LLM provider "
            "must be configured for the worker to generate replies."
        )

    configs = [primary]

    fallback = _config_from_env("LLM_FALLBACK")
    if fallback is not None:
        configs.append(fallback)

    return configs


def build_model_with_fallback(configs: list[LLMProviderConfig]) -> BaseChatModel:
    """
    Builds every configured model and, if more than one was given,
    wires them together with `.with_fallbacks()` so a failure of the
    first transparently falls through to the next.
    """
    models = [build_chat_model(config) for config in configs]
    primary, *rest = models
    if not rest:
        return primary
    return primary.with_fallbacks(rest)


def get_default_chat_model() -> BaseChatModel:
    """Convenience entry point used by worker.py."""
    return build_model_with_fallback(load_primary_and_fallback_configs())
