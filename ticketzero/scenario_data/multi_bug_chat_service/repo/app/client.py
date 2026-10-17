"""Thin wrapper around the Crusoe chat completions API."""
import os

from openai import OpenAI

from app import config


def _client() -> OpenAI:
    return OpenAI(
        base_url=config.BASE_URL,
        api_key=os.environ.get(config.API_KEY_ENV, ""),
        timeout=config.TIMEOUT_SECONDS,
    )


def ask(prompt: str) -> str:
    """Send one user prompt and return the assistant's reply text."""
    response = _client().chat.completions.create(
        model=config.MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=config.MAX_TOKENS,
    )
    return response.choices[0].message.content
