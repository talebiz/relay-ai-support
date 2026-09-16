"""
Provider-agnostic LLM factory for the AI worker.

Project brief, section 3: the LLM API cost is on us, so this project
targets whatever free-tier provider is available, and the bonus list
explicitly names automatic fallback to a second model when the first
one fails.

Design:

1. `LLMProvider` is a closed set of the TWO integration styles this
   worker knows how to build a `BaseChatModel` for. "openai_compatible"
   covers the common case: most free-tier providers (Groq, OpenRouter,
   local Ollama, etc.) expose an OpenAI-shaped `/chat/completions` API,
   so `langchain_openai.ChatOpenAI` pointed at a custom `base_url`
   works for all of them without a provider-specific package each.
   "google_genai" is the one provider that needs its own package,
   because Gemini's native API is not OpenAI-shaped.

   A direct Anthropic branch used to be here and was removed: this
   project is only ever pointed at an OpenAI-compatible endpoint or at
   Gemini, so `langchain-anthropic` was an installed package with no
   reachable code path. Note this does NOT mean Claude models are
   unusable - an aggregator like OpenRouter serves them over its
   OpenAI-shaped API, which is exactly what "openai_compatible" is
   for, and observability/pricing.py still carries their prices for
   that case. Adding the branch back would be a ~10-line change to
   `build_chat_model` plus one requirements line (see point 2).

2. `build_chat_model` is the ONLY place that imports a provider-specific
   LangChain integration package. The agent graph (graph/agent_graph.py)
   only ever depends on `BaseChatModel` - it does not know or care
   which concrete provider is behind it. Adding a fourth provider later
   means editing this one function, nothing else.

3. The fallback chain uses LangChain's own `.with_fallbacks()` rather
   than a hand-rolled try/except around every call site.
   `.with_fallbacks()` intercepts an exception raised by the primary
   model's `.invoke()` / `.stream()` (auth errors, HTTP timeouts, rate
   limits, provider outages) and transparently retries the SAME call
   against the next model in the list - callers just call
   `.invoke()`/`.stream()` on the object it returns and never see a
   difference.

   ORDER MATTERS, and getting it wrong fails loudly only once a
   fallback is actually configured - which is why there are two
   entry points here, not one:

       tools:    bind_tools_with_fallback(models, tools)
                 -> bind tools to EACH model, THEN wrap in fallbacks
       no tools: build_model_with_fallback(configs)

   `.with_fallbacks()` returns a `RunnableWithFallbacks`, which is a
   plain Runnable: it has `.invoke()` and `.stream()`, but it does NOT
   have `.bind_tools()` (verified: `hasattr(RunnableWithFallbacks,
   "bind_tools")` is False in langchain-core 0.3.28). So wrapping
   first and binding second raises AttributeError on every single
   agent run. With no fallback configured the wrapper is never created
   and the bug is invisible, so this is exactly the kind of latency
   that only shows up the day someone turns the bonus feature on.
   Binding first also means the FALLBACK model is given the same four
   tools as the primary - a fallback that silently lost its tools
   would answer from memory instead of calling the database, which is
   the failure the project brief calls out as worse than a wrong
   answer.

4. Configuration is read from environment variables so provider/model
   swaps never require a code change or redeploy - only a `.env` edit
   and a container restart (see .env.example: LLM_PRIMARY_* /
   LLM_FALLBACK_*).

5. Every branch of `build_chat_model` sets an explicit `timeout`
   (network calls have no default upper bound otherwise) and
   `max_retries=1` (let this project's own retry/backoff queues -
   messaging/topology.py - own the retry policy, not a second,
   invisible retry loop inside the LangChain client). Without a
   client-side timeout, a request that a provider or network never
   answers (e.g. a blocked/unreachable endpoint) blocks `run_agent`
   forever: the single worker process (`basic_qos(prefetch_count=1)`)
   never acks, never nacks, and never picks up another command - a
   silent full outage instead of the AGENT_RUN_FAILED + retry/DLQ path
   the project brief's reliability requirements (section 3(b)) already
   handle for every other kind of failure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

from langchain_core.language_models import BaseChatModel


class LLMProvider(str, Enum):
    OPENAI_COMPATIBLE = "openai_compatible"
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
            timeout=30,
            max_retries=1,
            # Part 7: ask the provider to include its own token usage
            # in the streaming response (this sends
            # `stream_options: {"include_usage": true}` on the wire).
            # Without it, an OpenAI-shaped endpoint reports NO usage at
            # all while streaming, and observability/tracing.py has to
            # fall back to counting tokens locally - which it will do,
            # but it then labels the run "estimated" rather than
            # "provider", because the brief asks for tokens counted for
            # real, not for an estimate presented as a measurement.
            #
            # A few OpenAI-compatible endpoints reject the unknown
            # `stream_options` field outright, so this is switchable:
            # set LLM_STREAM_USAGE=0 to turn it off for such a
            # provider. The cost ledger keeps working either way; only
            # its precision label changes.
            stream_usage=_stream_usage_enabled(),
        )

    if config.provider == LLMProvider.GOOGLE_GENAI:
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=config.model,
            google_api_key=config.api_key,
            temperature=config.temperature,
            timeout=30,
            max_retries=1,
        )

    raise ValueError(f"Unknown LLM provider: {config.provider!r}")


def _stream_usage_enabled() -> bool:
    """
    Whether to request provider-reported token usage on streaming
    responses. Defaults to ON: counting tokens for real (project brief
    section 7) is worth one extra field in the request body, and the
    only providers that need it off are the ones that reject unknown
    fields.
    """
    return os.getenv("LLM_STREAM_USAGE", "1").strip().lower() not in ("0", "false", "no")


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

    try:
        provider = LLMProvider(provider_name)
    except ValueError as exc:
        # Raised eagerly with the full list of valid values, because
        # the alternative is a bare "'x' is not a valid LLMProvider"
        # surfacing from inside an agent run - which then looks like a
        # model failure and gets routed to retry/DLQ, three times, for
        # what is really a one-word typo in .env. The most likely
        # cause is a config left over from when `anthropic` was still
        # a supported value (see this module's docstring, point 1).
        valid = ", ".join(member.value for member in LLMProvider)
        raise RuntimeError(
            f"{prefix}_PROVIDER={provider_name!r} is not a provider this worker "
            f"can build. Valid values: {valid}."
        ) from exc

    return LLMProviderConfig(
        provider=provider,
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
    if fallback is not None and _is_usable(fallback):
        configs.append(fallback)

    return configs


def _is_usable(config: LLMProviderConfig) -> bool:
    """
    Is this provider slot actually configured enough to call?

    Only applied to the FALLBACK slot. The reasoning: a half-configured
    fallback - a PROVIDER named but its API_KEY still blank - is worse
    than no fallback at all, because building that model raises at
    construction time, which happens inside `_get_chat_models()` on the
    first command and therefore takes down EVERY agent run, including
    the ones the perfectly-healthy primary could have served. A safety
    net that breaks the floor is not a safety net.

    A blank key is not automatically wrong, though: a self-hosted
    OpenAI-compatible endpoint (local Ollama, say) legitimately needs
    none. `base_url` is what distinguishes the two cases - if a slot
    names its own endpoint, we trust it and let it through; if it
    points at a hosted provider with no credential, we skip it and say
    so.

    The PRIMARY slot deliberately gets no such leniency: there is no
    degraded mode to fall back to, so a misconfigured primary must
    fail loudly rather than quietly.
    """
    if config.api_key or config.base_url:
        return True

    print(
        f"[llm] Ignoring LLM_FALLBACK_PROVIDER={config.provider.value!r}: "
        "no LLM_FALLBACK_API_KEY is set (and no LLM_FALLBACK_BASE_URL that "
        "would imply a keyless local endpoint). Running with the primary "
        "model only.",
        flush=True,
    )
    return False


def build_models(configs: list[LLMProviderConfig]) -> list[BaseChatModel]:
    """
    Build every configured model, UNWIRED - primary first.

    Returning the plain list (rather than something already wrapped in
    fallbacks) is what lets the caller bind tools before the fallback
    wrapper exists. See this module's docstring, point 3.
    """
    return [build_chat_model(config) for config in configs]


def get_default_chat_models() -> list[BaseChatModel]:
    """
    The models this deployment is configured to use, primary first -
    the entry point `worker.py` uses. One element when no fallback is
    configured, two when LLM_FALLBACK_PROVIDER is set.
    """
    return build_models(load_primary_and_fallback_configs())


def bind_tools_with_fallback(models: list[BaseChatModel], tools: list):
    """
    Bind `tools` to every model, THEN chain them with fallbacks.

    This is the ONLY correct order, and it lives in one function so no
    call site has to remember it - see this module's docstring, point
    3, for what goes wrong the other way round (AttributeError on
    `RunnableWithFallbacks.bind_tools`, on every run, but only once a
    fallback is configured).

    Returns a `BaseChatModel` when there is a single model and a
    `RunnableWithFallbacks` when there are several. Both expose the
    `.invoke()` the agent node calls, and both propagate the run
    config - so the part-7 callbacks (observability/tracing.py) still
    observe the fallback model's calls, and a run that failed over
    mid-way shows BOTH models in its trace rather than silently
    attributing everything to the primary.
    """
    if not models:
        raise RuntimeError("No LLM models configured - cannot build the agent.")

    bound = [model.bind_tools(tools) for model in models]
    primary, *rest = bound
    if not rest:
        return primary
    return primary.with_fallbacks(rest)


def build_model_with_fallback(configs: list[LLMProviderConfig]) -> BaseChatModel:
    """
    Fallback chain for the TOOL-LESS case only (the diagnostic scripts
    test_llm.py / a bare "can this provider answer at all" check).

    Do NOT use this for the agent: the object it returns cannot have
    tools bound to it afterwards. Use `bind_tools_with_fallback`
    instead - again, this module's docstring, point 3.
    """
    models = build_models(configs)
    primary, *rest = models
    if not rest:
        return primary
    return primary.with_fallbacks(rest)


def get_default_chat_model() -> BaseChatModel:
    """Tool-less convenience entry point - see build_model_with_fallback."""
    return build_model_with_fallback(load_primary_and_fallback_configs())
