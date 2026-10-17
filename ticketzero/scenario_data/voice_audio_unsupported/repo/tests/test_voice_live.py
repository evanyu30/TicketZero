import os

import pytest
import requests

from app import voice

CRUSOE_MODELS_URL = "https://api.inference.crusoecloud.com/v1/models"


def _live_catalog():
    key = os.environ.get("CRUSOE_API_KEY")
    assert key, "CRUSOE_API_KEY is required for live tests"
    r = requests.get(CRUSOE_MODELS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=30)
    r.raise_for_status()
    payload = r.json()
    return payload.get("data", []) if isinstance(payload, dict) else payload


@pytest.mark.live
def test_voicechat_model_listed_for_project():
    ids = {m.get("id", "").lower() for m in _live_catalog()}
    assert voice.VOICE_MODEL.lower() in ids


@pytest.mark.live
def test_send_audio_returns_reply():
    assert voice.VOICE_MODEL.lower() == "nvidia/nemotron-3-voicechat"
    out = voice.send_audio("tests/sample.wav")
    assert isinstance(out, str) and out.strip()
