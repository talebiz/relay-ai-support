"""
Turns token counts and Tavily searches into money - project brief,
section 7.

    "هزینه هر گفت‌وگو را در رابط کاربری نشان دهید. API رایگان است، اما
    «رایگان بود» پاسخ این پرسش نیست: token ها را واقعی بشمارید،
    اعتبارهای Tavily را اضافه کنید."

    (Show the cost of each conversation in the UI. The API is free,
    but "it was free" is not the answer to that question: count tokens
    for real, and add the Tavily credits.)

--- What "cost" means here, stated plainly ---

This project runs on a free-tier LLM endpoint, so the amount actually
charged to a credit card is, today, zero. The brief explicitly rejects
that as the answer. So what this module computes is the REFERENCE
cost: what this exact traffic - these exact, really-counted tokens -
would cost at published list prices for the model being used. That is
a number with real meaning:

  - it is proportional to actual resource consumption, so it rises
    when the agent loops more, retrieves more, or the prompt grows,
  - it is what the same system would cost the day the free tier ends
    or the model is swapped for a paid one,
  - and unlike "free", it cannot be used to avoid the question.

The displayed figure is therefore labeled as a reference cost in the
UI, not invented as a bill. Being explicit about that is the point; a
number presented as an invoice when it is an estimate would be a worse
answer than the honest one.

--- Tavily is priced separately, in its own unit ---

Tavily does not bill tokens, it bills CREDITS: one credit per basic
search, two per advanced search, with a free monthly allowance. The
brief names them as a separate thing to add, so they are tracked as a
separate count (`tavily_credits`) and converted to dollars at a
configurable per-credit rate - never folded into the token math, where
they would become invisible.

--- Everything here is configurable, because prices change ---

List prices move, and this file's defaults will age. Every number
below can be overridden from the environment without touching code:

    LLM_PRICE_TABLE_JSON              JSON object merged over the
                                      built-in table, e.g.
                                      {"my-model": [0.05, 0.08]}
                                      (USD per 1M input / output tokens)
    LLM_DEFAULT_PRICE_INPUT_PER_MTOK  price used when the model name
    LLM_DEFAULT_PRICE_OUTPUT_PER_MTOK matches nothing in the table
    TAVILY_USD_PER_CREDIT             dollars per Tavily credit
    TAVILY_CREDITS_PER_SEARCH         credits one search consumes

Whenever a fallback price is used instead of a table match, the span's
`detail.price_source` records that, so a reader can tell a priced
model from a guessed one.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal, ROUND_HALF_UP

# USD per 1,000,000 tokens, as (input, output). A small, deliberately
# short list of models this project is realistically pointed at - not
# an attempt to mirror every provider's price sheet, which would be
# stale within weeks. Matching is by case-insensitive substring
# (longest match wins), so "openai/gpt-4o-mini" and
# "gpt-4o-mini-2024-07-18" both resolve to the "gpt-4o-mini" row.
#
# Version numbers are normalized before matching (see `_normalize`),
# because the same model is spelled differently by different vendors:
# Anthropic's own id is "claude-3-5-sonnet" while an aggregator may
# serve the identical model as "anthropic/claude-3.5-sonnet". Without
# that, one spelling would price correctly and the other would quietly
# drop to the default rate.
DEFAULT_PRICE_TABLE: dict[str, tuple[str, str]] = {
    # OpenAI
    "gpt-4o-mini": ("0.15", "0.60"),
    "gpt-4o": ("2.50", "10.00"),
    "gpt-4.1-mini": ("0.40", "1.60"),
    "gpt-4.1-nano": ("0.10", "0.40"),
    "gpt-4.1": ("2.00", "8.00"),
    # Anthropic. Kept even though llm/factory.py has no Anthropic
    # branch any more: an aggregator like OpenRouter serves Claude over
    # its OpenAI-shaped API, so these rows are still reachable through
    # `openai_compatible` - and a reachable model with no price would
    # silently fall back to the default rate.
    "claude-3-5-haiku": ("0.80", "4.00"),
    "claude-3-5-sonnet": ("3.00", "15.00"),
    "claude-3-haiku": ("0.25", "1.25"),
    # Google
    "gemini-1.5-flash-8b": ("0.0375", "0.15"),
    "gemini-1.5-flash": ("0.075", "0.30"),
    "gemini-1.5-pro": ("1.25", "5.00"),
    "gemini-2.0-flash": ("0.10", "0.40"),
    # Groq / open-weight models commonly used on free tiers
    "llama-3.1-8b-instant": ("0.05", "0.08"),
    "llama-3.3-70b-versatile": ("0.59", "0.79"),
    "llama3-8b": ("0.05", "0.08"),
    "llama3-70b": ("0.59", "0.79"),
    "mixtral-8x7b": ("0.24", "0.24"),
    "gemma2-9b": ("0.20", "0.20"),
    "qwen": ("0.10", "0.20"),
    "deepseek": ("0.14", "0.28"),
}

# Six decimal places everywhere: a single cheap call can legitimately
# cost 0.000042 USD, and rounding to cents would silently turn this
# whole feature into a column of zeros.
MONEY_PRECISION = Decimal("0.000001")


def _decimal_env(name: str, default: str) -> Decimal:
    raw = os.getenv(name, "").strip()
    try:
        return Decimal(raw) if raw else Decimal(default)
    except Exception:  # noqa: BLE001 - a malformed price must not break a run
        return Decimal(default)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _load_price_table() -> dict[str, tuple[Decimal, Decimal]]:
    table = {
        key: (Decimal(value[0]), Decimal(value[1]))
        for key, value in DEFAULT_PRICE_TABLE.items()
    }

    raw = os.getenv("LLM_PRICE_TABLE_JSON", "").strip()
    if raw:
        try:
            for key, value in json.loads(raw).items():
                table[key.lower()] = (Decimal(str(value[0])), Decimal(str(value[1])))
        except Exception as exc:  # noqa: BLE001
            print(f"[pricing] Ignoring malformed LLM_PRICE_TABLE_JSON: {exc}", flush=True)

    return table


_PRICE_TABLE = _load_price_table()


def _normalize(name: str) -> str:
    """
    Lowercase, and collapse version separators so "claude-3.5-sonnet"
    and "claude-3-5-sonnet" are the same key. See DEFAULT_PRICE_TABLE's
    comment for why this matters.
    """
    return name.lower().replace(".", "-")


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)


def resolve_model_price(model_name: str | None) -> tuple[Decimal, Decimal, str]:
    """
    Find the (input, output) price per 1M tokens for a model name.

    Returns the two prices plus a `price_source` string - "table" when
    the model was recognized, "default" when it was not and the
    configured fallback price was used. That string is stored on every
    llm span so nothing in this ledger is silently guessed.
    """
    name = _normalize(model_name or "")
    best_key = ""
    for key in _PRICE_TABLE:
        if _normalize(key) in name and len(key) > len(best_key):
            best_key = key

    if best_key:
        input_price, output_price = _PRICE_TABLE[best_key]
        return input_price, output_price, f"table:{best_key}"

    return (
        _decimal_env("LLM_DEFAULT_PRICE_INPUT_PER_MTOK", "0.15"),
        _decimal_env("LLM_DEFAULT_PRICE_OUTPUT_PER_MTOK", "0.60"),
        "default",
    )


def llm_cost_usd(model_name: str | None, input_tokens: int, output_tokens: int) -> tuple[Decimal, str]:
    """Reference cost of one model call, plus the price source used."""
    input_price, output_price, price_source = resolve_model_price(model_name)
    million = Decimal(1_000_000)
    cost = (Decimal(max(input_tokens, 0)) / million) * input_price + (
        Decimal(max(output_tokens, 0)) / million
    ) * output_price
    return quantize_money(cost), price_source


def tavily_credits_per_search(search_depth: str = "basic") -> int:
    """
    Credits one Tavily search consumes. Tavily charges 1 credit for a
    basic search and 2 for an advanced one; tools/web_search_tools.py
    uses `search_depth: "basic"`, so the default matches what this
    project actually sends.
    """
    base = _int_env("TAVILY_CREDITS_PER_SEARCH", 1)
    return base * 2 if search_depth == "advanced" else base


def tavily_cost_usd(credits: int) -> Decimal:
    """
    Dollar value of consumed Tavily credits.

    Default rate is 0.008 USD/credit, roughly Tavily's paid-plan rate
    at the time of writing. On the free tier the charge is zero, which
    is exactly why the CREDIT COUNT is surfaced alongside the dollar
    figure in the UI: the credits are consumed and finite (a monthly
    allowance) whether or not they are billed, so the count is the
    honest number and the dollars are the reference value.
    """
    rate = _decimal_env("TAVILY_USD_PER_CREDIT", "0.008")
    return quantize_money(Decimal(max(credits, 0)) * rate)
