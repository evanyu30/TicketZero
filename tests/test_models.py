"""Model resolution against catalog naming variants seen on Crusoe."""
from ticketzero import models

# Shape of a real Crusoe catalog: Super and Nano carry an extra "NVIDIA-" prefix, plus look-alike variants.
LIVE_LIKE_CATALOG = [{"id": i, "supported_parameters": ["tools", "response_format"],
                      "top_provider": {"max_completion_tokens": 262144}} for i in [
    "nvidia/Nemotron-3.5-Lightning-30B-A3B",
    "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B",
    "nvidia/NVIDIA-Nemotron-3.5-Super-VL",
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    "nvidia/Nemotron-3-Nano-Omni-Reasoning-30B-A3B",
    "openai/gpt-oss-120b",
]]


def test_resolves_nvidia_prefixed_ids_and_keeps_exact_returned_id(monkeypatch):
    monkeypatch.setattr(models, "fetch_crusoe_models", lambda: LIVE_LIKE_CATALOG)
    reg = models.ModelRegistry()
    reg.resolve()
    assert reg.tiers == {
        "super": "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B",
        "lightning": "nvidia/Nemotron-3.5-Lightning-30B-A3B",
        "nano": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
        "ultra": None,
    }
    assert reg.roles["planner"] == reg.roles["verifier"] == "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B"
    assert reg.roles["fixer"] == "nvidia/Nemotron-3.5-Lightning-30B-A3B"
    assert reg.roles["gate"] == "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
    assert reg.roles["planner_root"] == reg.roles["planner"]   # Ultra not listed
    assert reg.notes == [] and reg.ok()


def test_canonical_id():
    assert models.canonical_model_id("nvidia/NVIDIA-Nemotron-3-Super-120B-A12B") == "nemotron-3-super-120b-a12b"
    assert models.canonical_model_id("nvidia/Nemotron-3-Super-120B-A12B") == "nemotron-3-super-120b-a12b"
    assert models.canonical_model_id("nvidia/NVIDIA-Nemotron-3.5-Super-VL") != "nemotron-3-super-120b-a12b"
