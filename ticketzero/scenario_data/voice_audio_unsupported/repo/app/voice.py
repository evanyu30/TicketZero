"""Send recorded audio to Nemotron VoiceChat and return its reply."""
import base64
import os

import requests

BASE_URL = "https://api.inference.crusoecloud.com/v1"
VOICE_MODEL = "nvidia/Nemotron-3-VoiceChat"


def send_audio(path: str) -> str:
    with open(path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode()
    # Guessed payload: no published request format for VoiceChat.
    payload = {
        "model": VOICE_MODEL,
        "messages": [{"role": "user", "content": [{"type": "audio", "audio": audio_b64}]}],
    }
    r = requests.post(
        f"{BASE_URL}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {os.environ.get('CRUSOE_API_KEY', '')}"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]
