"""Runtime settings for the support chat service.

Each setting below is read by app/client.py.
"""

# Crusoe Managed Inference endpoint (OpenAI-compatible).
# The client appends /chat/completions to this URL.
BASE_URL = "https://api.crusoe.ai/v1"

# Chat model served for the support bot.
# Must be a model that is currently available on Crusoe.
MODEL = "meta-llama/Llama-3.3-70B-Instruct"

# Upper bound on generated tokens per request.
# Reasoning models count thinking tokens against this limit.
MAX_TOKENS = 64

# Name of the environment variable that holds the API key.
API_KEY_ENV = "CRUSOE_API_KEY"

# Per-request timeout in seconds.
TIMEOUT_SECONDS = 60
