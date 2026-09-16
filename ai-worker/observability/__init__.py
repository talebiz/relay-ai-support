"""
Observability for the AI worker - project brief, section 7.

Three modules, one job each:

    pricing.py         turns token counts and Tavily searches into money.
    tracing.py         the LangChain callback handler that observes every
                       model call and every tool call inside one run.
    langfuse_client.py optional Langfuse handler, keyed by correlation_id.

Nothing in this package is allowed to break a run. Observability that
can take down the thing it observes is worse than no observability, so
every entry point here is defensive: a missing key, an unreachable
Langfuse, a provider that reports no usage, or a malformed tool result
degrades to a less precise trace, never to a failed reply.
"""
