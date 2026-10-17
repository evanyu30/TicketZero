"""A scripted stand-in for Crusoe Managed Inference, so whole runs execute offline.

Only the network call (CrusoeLLM._create) and the live model catalog are replaced; budgets, worktrees, merges,
pytest verification, the database and the API all run for real.
"""
import difflib
import json
import threading

from openai.types.chat import ChatCompletion

from fake_nemotron_env import SECRET  # noqa: F401  (re-exported for tests)
from ticketzero import prompts
from ticketzero.scenarios import SCENARIOS

CATALOG = [
    {"id": model_id, "context_length": 131072, "supported_parameters": ["tools", "response_format", "max_tokens"],
     "top_provider": {"max_completion_tokens": 32768}}
    for model_id in ["nvidia/Nemotron-3-Super-120B-A12B", "nvidia/Nemotron-3.5-Lightning-30B-A3B",
                     "nvidia/Nemotron-3-Nano-30B-A3B"]
]

CONFIG_PY = (SCENARIOS["multi_bug_chat_service"]["folder"] / "app/config.py").read_text()
AGENT_PY = (SCENARIOS["tool_loop_ordering"]["folder"] / "app/agent.py").read_text()
VOICE_PY = (SCENARIOS["voice_audio_unsupported"]["folder"] / "app/voice.py").read_text()

# Scenario B: a naive patch that fixes ordering only, and the complete fix.
ORDER_FIX = AGENT_PY.replace("""        if message.tool_calls:
            for call""", """        if message.tool_calls:
            messages.append({"role": "assistant", "content": message.content or "", "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in message.tool_calls]})
            for call""")
FULL_FIX = ORDER_FIX.replace("""    for _ in range(max_steps):""", """    retried = False
    for _ in range(max_steps):""").replace("""            continue
        return message.content""", """            continue
        if choice.finish_reason == "length" and not message.content and not retried:
            retried = True
            max_tokens *= 2
            continue
        return message.content""")


def udiff(path: str, old: str, new: str) -> str:
    body = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}"))
    return f"diff --git a/{path} b/{path}\n" + body


class FakeNemotron:
    """mode: "A" | "B" | "C" | "budget" | "bad_json" selects the scripted behavior."""

    def __init__(self, mode: str):
        self.mode = mode
        self.calls = 0
        self.lock = threading.Lock()
        self.prompts_seen: list[str] = []

    def install(self, monkeypatch) -> None:
        from ticketzero import llm, models
        monkeypatch.setattr(models, "fetch_crusoe_models", lambda: CATALOG)
        fake = self

        def fake_create(_self, kwargs):
            return fake.create(kwargs)

        monkeypatch.setattr(llm.CrusoeLLM, "_create", fake_create)
        models.REGISTRY.resolve()

    def create(self, kwargs: dict):
        with self.lock:
            self.calls += 1
            self.prompts_seen.append(json.dumps(kwargs["messages"], default=str))
        r = self.reply(kwargs)
        msg = {"role": "assistant", "content": r.get("content"), "reasoning_content": "thinking..."}
        if r.get("tool_calls"):
            msg["tool_calls"] = r["tool_calls"]
        completion = ChatCompletion.model_validate({
            "id": "fake", "object": "chat.completion", "created": 0, "model": kwargs["model"],
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": "tool_calls" if r.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
                      "prompt_tokens_details": {"cached_tokens": 600}}})
        return completion, {"x-ratelimit-remaining-requests": "29"}

    # -- scripted behavior per role --
    def reply(self, kwargs: dict) -> dict:
        msgs = kwargs["messages"]
        system, user = msgs[0]["content"], msgs[1]["content"] if len(msgs) > 1 else ""
        scen = {"budget": "A", "bad_json": "B"}.get(self.mode, self.mode)
        if system == prompts.TRIAGE_SYSTEM:
            return {"content": json.dumps({"symptoms": ["404", "429", "empty"] if scen == "A" else ["400"],
                                           "independent_symptoms": 3 if scen == "A" else 1, "status_codes": [404]})}
        if system == prompts.PLANNER_SYSTEM:
            return self.planner(scen, user)
        if system == prompts.INVESTIGATOR_SYSTEM:
            return self.investigator(kwargs, msgs)
        if system == prompts.GATE_SYSTEM:
            if scen == "C":
                return {"content": json.dumps({"enough": False, "missing": "documented VoiceChat audio request format"})}
            return {"content": json.dumps({"enough": True})}
        if system == prompts.FIXER_SYSTEM:
            return self.fixer(scen, user.split("# Your task")[-1])
        if system == prompts.VERIFIER_SYSTEM:
            return {"content": json.dumps({"veto": False, "reason": "minimal correct fix"})}
        if system == prompts.ESCALATION_SYSTEM:
            return {"content": json.dumps({"checks": ["Contact Crusoe support about the VoiceChat request format."]})}
        raise AssertionError("unknown role prompt")

    def planner(self, scen: str, user: str) -> dict:
        ids = [l[2:].split(" ")[0] for l in user.splitlines() if l.startswith("- tests/")]
        if "define_success" in user.split("# Mode")[-1]:
            # Fenced JSON exercises the lenient extractor.
            return {"content": "```json\n" + json.dumps({"criteria": [{"id": "C1", "text": "ok", "test_ids": ids[:2]}]})
                               + "\n```"}
        if self.mode == "budget":
            return {"content": json.dumps({"kind": "split", "subtasks": [{"title": "x"}, {"title": "y"}]})}
        if self.mode == "bad_json":
            return {"content": "I think it should be atomic."}
        if scen == "A" and "depth 0 of" in user:
            return {"content": json.dumps({"kind": "split", "parallel": True, "subtasks": [
                {"title": "Fix API host", "goal": "point BASE_URL at the current host",
                 "test_ids": ["tests/test_config.py::test_base_url_is_current_crusoe_host"]},
                {"title": "Fix model ID", "goal": "use an available Nemotron",
                 "test_ids": ["tests/test_config.py::test_model_is_nvidia_nemotron",
                              "tests/test_live.py::test_model_listed_in_live_catalog"]},
                {"title": "Fix token budget", "goal": "raise MAX_TOKENS",
                 "test_ids": ["tests/test_config.py::test_max_tokens_leaves_room_for_reasoning"]}]})}
        return {"content": json.dumps({"kind": "atomic", "approach": "fix it"})}

    def investigator(self, kwargs: dict, msgs: list[dict]) -> dict:
        if "tools" in kwargs and not any(m["role"] == "tool" for m in msgs):
            return {"tool_calls": [
                {"id": "call_a", "type": "function", "function": {"name": "list_crusoe_models", "arguments": "{}"}},
                {"id": "call_b", "type": "function",
                 "function": {"name": "read_file", "arguments": json.dumps({"path": "../../etc/passwd"})}},
                {"id": "call_c", "type": "function",
                 "function": {"name": "web_search", "arguments": json.dumps({"query": "crusoe llama deprecation"})}}]}
        for i, m in enumerate(msgs):   # tool messages must follow an assistant message carrying tool_calls
            if m["role"] == "tool":
                prev = [x for x in msgs[:i] if x["role"] != "tool"][-1]
                assert prev["role"] == "assistant" and prev.get("tool_calls"), "tool message ordering broken"
        return {"content": json.dumps({"root_cause": "see config", "confidence": 0.9, "proposed_change": "edit",
                                       "evidence": [{"kind": "models_api", "ref": "nvidia/Nemotron-3-Super-120B-A12B",
                                                     "detail": "listed"}]})}

    def fixer(self, scen: str, task: str) -> dict:
        if scen == "A":
            if "Fix API host" in task:
                new = CONFIG_PY.replace("https://api.crusoe.ai/v1", "https://api.inference.crusoecloud.com/v1")
            elif "Fix model ID" in task:
                new = CONFIG_PY.replace("meta-llama/Llama-3.3-70B-Instruct", "nvidia/Nemotron-3-Super-120B-A12B")
            else:
                new = CONFIG_PY.replace("MAX_TOKENS = 64", "MAX_TOKENS = 4096")
            return {"content": json.dumps({"summary": "config fix", "diff": udiff("app/config.py", CONFIG_PY, new)})}
        if scen == "B":
            if "Fix failed verification" in task:   # fenced diff exercises the fixer fallback
                return {"content": "```diff\n" + udiff("app/agent.py", AGENT_PY, FULL_FIX) + "```"}
            return {"content": json.dumps({"summary": "append assistant message",
                                           "diff": udiff("app/agent.py", AGENT_PY, ORDER_FIX)})}
        guessed = VOICE_PY.replace('"type": "audio"', '"type": "input_audio"')
        return {"content": json.dumps({"summary": "guess", "diff": udiff("app/voice.py", VOICE_PY, guessed)})}
