import os
import sys
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

try:
    from llm.factory import get_default_chat_model
    from langchain_core.messages import HumanMessage
except ImportError as exc:
    print(f"ImportError: Failed to load required packages: {exc}")
    sys.exit(1)

print("=== 1. Checking Environment Variables ===")
provider = os.getenv("LLM_PRIMARY_PROVIDER")
model_name = os.getenv("LLM_PRIMARY_MODEL")
api_key = os.getenv("LLM_PRIMARY_API_KEY", "")

masked_key = (("*" * 8) + api_key[-4:]) if len(api_key) > 4 else "NOT_SET"

print(f"Provider : {provider}")
print(f"Model    : {model_name}")
print(f"API Key  : {masked_key}")
print("=========================================\n")

if not api_key:
    print("Error: LLM_PRIMARY_API_KEY is empty. Please set it in your .env file.")
    sys.exit(1)

print("Initializing chat model from factory...")
try:
    model = get_default_chat_model()
    print("Chat model successfully configured.\n")
except Exception as exc:
    print(f"Failed to initialize model: {exc}")
    sys.exit(1)

test_prompt = "Hello! In one short sentence, identify your model and confirm you received this message."
print(f"Sending test prompt to model...\nPrompt: '{test_prompt}'\nStreaming response:")

try:
    for chunk in model.stream([HumanMessage(content=test_prompt)]):
        print(chunk.content or "", end="", flush=True)
    print("\n\nSuccess: Model connection and streaming test passed.")
except Exception as exc:
    print(f"\n\nConnection failed: {type(exc).__name__}: {exc}")
    print("\nTroubleshooting tips:")
    print("  - 403 / 'User location is not supported': Region restricted by provider. Configure a proxy or VPN for Docker.")
    print("  - 401 / 'Invalid API Key': Verify the API key in your .env file.")
    print("  - 429 / 'ResourceExhausted': Rate limit exceeded or quota exhausted for the configured tier.")
    sys.exit(1)