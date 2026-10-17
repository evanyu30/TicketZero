import wave
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def sample_wav():
    """Generate a tiny silent WAV (0.5 s, 16 kHz, mono, 16-bit) for the voice tests."""
    path = Path("tests/sample.wav")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 8000)
    return path
