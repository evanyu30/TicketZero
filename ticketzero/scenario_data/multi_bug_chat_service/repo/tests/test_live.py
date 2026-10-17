import os

import pytest
import requests

from app import config
from app.client import ask

CRUSOE_MODELS_URL = "https://api.inference.crusoecloud.com/v1/models"


def _live_catalog():
    key = os.environ.get("CRUSOE_API_KEY")
    assert key, "CRUSOE_API_KEY is required for live tests"
    r = requests.get(CRUSOE_MODELS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=30)
    r.raise_for_status()
    payload = r.json()
    return payload.get("data", []) if isinstance(payload, dict) else payload


@pytest.mark.live
def test_model_listed_in_live_catalog():
    ids = {m.get("id") for m in _live_catalog()}
    assert config.MODEL in ids


@pytest.mark.live
def test_ask_smoke_returns_pong():
    out = ask("Reply with exactly: pong")
    assert out and out.strip()
    assert "pong" in out.lower()
