"""Nemotron model routing (F7): resolve preferred IDs against the live /v1/models catalog, plus cost estimates."""
import threading
import time
from typing import Optional

import requests

from . import config
from .errors import LLMError
from .util import now_iso, redact

PREFERRED_MODELS = {
    "super": "nvidia/Nemotron-3-Super-120B-A12B",
    "lightning": "nvidia/nemotron-3.5-lightning-30b-a3b",
    "nano": "nvidia/Nemotron-3-Nano-30B-A3B",
    "ultra": "nvidia/NVIDIA-Nemotron-3-Ultra-550B",
}
ROLE_TIER = {
    "planner": "super",           # define success + decompose, replanning, parent aggregation
    "investigator": "lightning",  # Crusoe reasoning + You.com research
    "fixer": "lightning",         # propose and apply safe fix
    "gate": "nano",               # evidence gate, log parsing, ticket triage
    "verifier": "super",          # independent verifier critic, must differ from fixer
}
TIER_FALLBACK = {
    "super": ["super", "lightning", "nano"],
    "lightning": ["lightning", "nano", "super"],
    "nano": ["nano", "lightning", "super"],
}
COST_PER_M = {  # USD per 1M tokens: input, output, cached
    "super": (0.30, 2.40, 0.15),
    "lightning": (0.05, 0.20, 0.03),
    "nano": (0.05, 0.20, 0.03),
    "ultra": (1.00, 3.20, 0.25),
}


def model_family(model_id: str) -> str:
    m = (model_id or "").lower()
    for fam in ("ultra", "super", "lightning", "nano"):
        if fam in m:
            return fam
    return "super"


def canonical_model_id(model_id: str) -> str:
    """Case-insensitive, and tolerant of the optional "NVIDIA-" name prefix Crusoe uses for some models:
    nvidia/NVIDIA-Nemotron-3-Super-120B-A12B and nvidia/Nemotron-3-Super-120B-A12B both -> nemotron-3-super-120b-a12b."""
    m = (model_id or "").lower().removeprefix("nvidia/")
    return m.removeprefix("nvidia-")


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int) -> float:
    """Billed input = (prompt - cached) at the input rate plus cached at the cached rate."""
    rin, rout, rcache = COST_PER_M[model_family(model)]
    billed_in = max(0, prompt_tokens - cached_tokens)
    return (billed_in * rin + cached_tokens * rcache + completion_tokens * rout) / 1_000_000


def fetch_crusoe_models() -> list[dict]:
    """Live GET /v1/models. Used at startup, as an agent tool, and by demo acceptance tests."""
    if not config.CRUSOE_API_KEY:
        raise LLMError("CRUSOE_API_KEY is not set")
    r = requests.get(f"{config.CRUSOE_BASE_URL}/models",
                     headers={"Authorization": f"Bearer {config.CRUSOE_API_KEY}"}, timeout=30)
    if r.status_code >= 400:
        raise LLMError(f"GET /v1/models returned HTTP {r.status_code}: {redact(r.text[:500])}")
    payload = r.json()
    data = payload.get("data", []) if isinstance(payload, dict) else payload
    return [m for m in data if isinstance(m, dict) and m.get("id")]


class ModelRegistry:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.info: dict[str, dict] = {}       # exact id -> {supported_parameters, context_length, max_completion_tokens}
        self.tiers: dict[str, Optional[str]] = {t: None for t in PREFERRED_MODELS}
        self.roles: dict[str, Optional[str]] = {r: None for r in list(ROLE_TIER) + ["planner_root"]}
        self.error: Optional[str] = None
        self.notes: list[str] = []
        self.resolved_at: Optional[str] = None
        self.last_attempt = 0.0

    def resolve(self) -> None:
        """Match preferred IDs against /v1/models and keep the exact returned id.
        Tries a case-insensitive exact match first, then ignores the optional "NVIDIA-" prefix."""
        self.last_attempt = time.monotonic()
        try:
            catalog = fetch_crusoe_models()
        except Exception as e:  # backend still starts; /health reports ok: false
            with self.lock:
                self.error = redact(str(e))
            return
        by_lower = {m["id"].lower(): m for m in catalog}
        by_canonical = {canonical_model_id(m["id"]): m for m in catalog}
        info: dict[str, dict] = {}
        tiers: dict[str, Optional[str]] = {}
        for tier, pref in PREFERRED_MODELS.items():
            m = by_lower.get(pref.lower()) or by_canonical.get(canonical_model_id(pref))
            tiers[tier] = m["id"] if m else None
            if m:
                top = m.get("top_provider") or {}
                info[m["id"]] = {
                    "supported_parameters": list(m.get("supported_parameters") or []),
                    "context_length": m.get("context_length"),
                    "max_completion_tokens": top.get("max_completion_tokens") if isinstance(top, dict) else None,
                    "features": m.get("features"),
                }
        roles: dict[str, Optional[str]] = {}
        notes: list[str] = []
        for role, tier in ROLE_TIER.items():
            roles[role] = next((tiers[t] for t in TIER_FALLBACK[tier] if tiers.get(t)), None)
            if roles[role] and roles[role] != tiers.get(tier):
                notes.append(f"role {role}: preferred {PREFERRED_MODELS[tier]} missing, fell back to {roles[role]}")
        # The verifier must differ from the fixer model.
        if roles.get("verifier") and roles["verifier"] == roles.get("fixer"):
            alt = next((tiers[t] for t in ("super", "nano", "lightning")
                        if tiers.get(t) and tiers[t] != roles["fixer"]), None)
            if alt:
                roles["verifier"] = alt
            else:
                notes.append("only one Nemotron model available: verifier critic shares the fixer model")
        roles["planner_root"] = tiers.get("ultra") or roles.get("planner")
        with self.lock:
            self.info, self.tiers, self.roles, self.notes = info, tiers, roles, notes
            self.error = None if any(roles.values()) else "no Nemotron model from the routing table is listed by /v1/models"
            self.resolved_at = now_iso()

    def ok(self) -> bool:
        return bool(config.CRUSOE_API_KEY) and all(self.roles.get(r) for r in ROLE_TIER)

    def model_for(self, role: str) -> Optional[str]:
        return self.roles.get(role)

    def supported(self, model: str) -> list[str]:
        return self.info.get(model, {}).get("supported_parameters", [])

    def max_completion(self, model: str) -> Optional[int]:
        v = self.info.get(model, {}).get("max_completion_tokens")
        return int(v) if isinstance(v, (int, float)) and v > 0 else None


REGISTRY = ModelRegistry()
