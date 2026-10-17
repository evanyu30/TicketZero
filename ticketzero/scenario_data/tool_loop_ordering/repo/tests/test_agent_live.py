import os

import pytest
import requests
from openai import OpenAI

from app.agent import run_tool_loop

CRUSOE_MODELS_URL = "https://api.inference.crusoecloud.com/v1/models"


def _live_catalog():
    key = os.environ.get("CRUSOE_API_KEY")
    assert key, "CRUSOE_API_KEY is required for live tests"
    r = requests.get(CRUSOE_MODELS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=30)
    r.raise_for_status()
    payload = r.json()
    return payload.get("data", []) if isinstance(payload, dict) else payload


@pytest.mark.live
def test_live_tool_call_roundtrip_on_nemotron():
    catalog = _live_catalog()
    candidates = sorted(m["id"] for m in catalog if "nemotron" in m.get("id", "").lower()
                        and "tools" in (m.get("supported_parameters") or []))
    assert candidates, "no Nemotron model with tool support is listed"
    client = OpenAI(base_url="https://api.inference.crusoecloud.com/v1",
                    api_key=os.environ["CRUSOE_API_KEY"])
    seen = []

    def add(a, b):
        seen.append((a, b))
        return a + b

    tools = [{"type": "function", "function": {"name": "add", "description": "Add two integers",
              "parameters": {"type": "object", "properties": {"a": {"type": "integer"},
              "b": {"type": "integer"}}, "required": ["a", "b"]}}}]
    out = run_tool_loop(client, candidates[0], "Use the add tool to compute 17 + 25, then reply with "
                        "the number only.", tools, {"add": add})
    assert seen, "the model never called the tool"
    assert out and "42" in out
