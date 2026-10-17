"""Fog City Concierge: a small customer app built on Crusoe Managed Inference.

ask(question)       -> answers weather questions by calling the get_weather tool
weather_card(city)  -> returns a JSON weather card: {"city", "temp_c", "advice"}
"""
import json
import os

from openai import OpenAI

BASE_URL = "https://api.inference.crusoecloud.com/v1"
API_KEY_ENV = "CRUSOE_API_KEY"
MODEL = "test-model"
MAX_TOKENS = 4_000_000
CARD_MAX_TOKENS = 1024

client = OpenAI(base_url=BASE_URL, api_key=os.environ.get(API_KEY_ENV))

TOOL_CALLS = []  # every city get_weather was called with (the tests read this)


def get_weather(city):
    """Fake weather service. Returns a fixed reading no model could guess."""
    TOOL_CALLS.append(city)
    return {"city": city, "temp_c": 13.7, "conditions": "fog"}


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]

SYSTEM = (
    "You are a city concierge. For any weather question, call get_weather. "
    "Always state the exact temperature the tool returns, in Celsius."
)


def ask(question):
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": question},
    ]
    for _ in range(3):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            max_tokens=MAX_TOKENS,
        )
        msg = response.choices[0].message
        if not msg.tool_calls:
            return msg.content or ""
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
            }
        )
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            result = get_weather(**args)
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)}
            )
    return ""


def _extract_json(text):
    """Pull the JSON object out of a reply, even if wrapped in ``` fences."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object in model reply: {text!r}")
    return json.loads(text[start : end + 1])


def weather_card(city):
    weather = get_weather(city)
    prompt = (
        "Return ONLY a JSON object with keys city (string), temp_c (number), "
        "advice (one short sentence). Use this data: " + json.dumps(weather)
    )
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=CARD_MAX_TOKENS,
    )
    return _extract_json(response.choices[0].message.content or "")
