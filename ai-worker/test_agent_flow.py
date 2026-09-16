"""
Diagnostic test: runs the FULL agent graph (with tools bound)
against the configured LLM, exactly as worker.py does.

If test_llm.py passes but this fails, the problem is in tool-binding
or graph execution, not in the basic LLM connection.
"""
import os
import sys
import json
import time

from dotenv import load_dotenv
load_dotenv()

from llm.factory import get_default_chat_models
from graph.agent_graph import build_agent_graph, build_initial_messages, AGENT_RECURSION_LIMIT

print("=== Agent Flow Diagnostic ===\n")

provider = os.getenv("LLM_PRIMARY_PROVIDER")
model_name = os.getenv("LLM_PRIMARY_MODEL")
print(f"Provider : {provider}")
print(f"Model    : {model_name}\n")

print("[1/4] Building chat model...")
try:
    models = get_default_chat_models()
    print("  OK\n")
except Exception as e:
    print(f"  FAILED: {e}")
    sys.exit(1)

# Use a fake acting_user_id=1 (alice). The tools will try DB queries
# but that's fine — we just want to see if the model + tools integration works.
ACTING_USER_ID = 1

print("[2/4] Building agent graph with tools...")
try:
    graph = build_agent_graph(models, ACTING_USER_ID)
    print("  OK\n")
except Exception as e:
    print(f"  FAILED: {e}")
    sys.exit(1)

test_message = "سلام. وضعیت سفارش من چیست؟"
messages = build_initial_messages([], test_message)
graph_config = {
    "configurable": {"thread_id": "test-diagnostic"},
    "recursion_limit": AGENT_RECURSION_LIMIT,
}

print(f"[3/4] Running graph.stream() with message: '{test_message}'")
print("  stream_mode=['messages', 'updates']")
print("  Waiting for response (timeout: 60s)...\n")

start = time.time()
items_received = 0
text_parts = []
tool_calls_seen = []

try:
    for stream_kind, chunk in graph.stream(
        {"messages": messages},
        config=graph_config,
        stream_mode=["messages", "updates"],
    ):
        elapsed = time.time() - start
        items_received += 1

        if stream_kind == "messages":
            message_chunk, _metadata = chunk
            msg_type = getattr(message_chunk, "type", "?")
            msg_class = type(message_chunk).__name__
            content = message_chunk.content
            tool_calls_attr = getattr(message_chunk, "tool_calls", None)
            additional = getattr(message_chunk, "additional_kwargs", {})
            print(f"  [{elapsed:.1f}s] messages: class={msg_class} type={msg_type} content={content!r} tool_calls={tool_calls_attr} additional_kwargs={additional}", flush=True)
            if content:
                text_parts.append(content)

        elif stream_kind == "updates":
            for node_name, node_output in chunk.items():
                print(f"  [{elapsed:.1f}s] updates: node={node_name}", flush=True)
                for msg in node_output.get("messages", []):
                    msg_class = type(msg).__name__
                    msg_type = getattr(msg, "type", "?")
                    content = msg.content
                    tc = getattr(msg, "tool_calls", None)
                    additional = getattr(msg, "additional_kwargs", {})
                    print(f"    class={msg_class} type={msg_type} content={content!r:.200} tool_calls={tc} additional_kwargs={additional}", flush=True)
                    if tc:
                        for call in tc:
                            tool_calls_seen.append(call.get("name"))

except Exception as e:
    elapsed = time.time() - start
    print(f"\n\n  FAILED after {elapsed:.1f}s: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

elapsed = time.time() - start
reply_text = "".join(text_parts)

print(f"\n\n[4/4] Results:")
print(f"  Total time     : {elapsed:.1f}s")
print(f"  Stream items   : {items_received}")
print(f"  Tool calls     : {tool_calls_seen or 'none'}")
print(f"  Reply length   : {len(reply_text)} chars")
print(f"  Reply preview  : {reply_text[:300]}")
print(f"\n{'='*40}")
if reply_text:
    print("SUCCESS: Agent graph produced a response.")
else:
    print("WARNING: Agent graph finished but produced no text.")
