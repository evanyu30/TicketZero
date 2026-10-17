"""Rate-limit handling: a 429 pauses the whole model and the call is retried."""
import httpx2 as httpx  # the HTTP library this openai SDK version is built on
import openai

from fake_nemotron import FakeNemotron
from ticketzero.budget import NodeBudget, NodeCtx, RunBudget, RunState
from ticketzero.llm import LLM
from ticketzero.scenarios import SCENARIOS
from ticketzero.util import new_id


def rate_limited() -> openai.RateLimitError:
    response = httpx.Response(429, request=httpx.Request("POST", "http://test"), headers={"retry-after": "0"})
    body = {"error": {"message": "Too many requests. Reason: max_requests_limit (orchestrator)"}}
    return openai.RateLimitError("Error code: 429", response=response, body=body)


def test_429_pauses_model_then_retries(client, monkeypatch):
    fake = FakeNemotron("A")
    fake.install(monkeypatch)
    failures = [rate_limited(), rate_limited()]
    real_create = fake.create

    def flaky_create(_self, kwargs):
        if failures:
            raise failures.pop(0)
        return real_create(kwargs)

    monkeypatch.setattr(type(LLM), "_create", flaky_create)
    sc = SCENARIOS["multi_bug_chat_service"]
    run = RunState(new_id("rllm"), "A", sc["ticket"], sc["folder"], {}, RunBudget(), {})
    nc = NodeCtx(run, new_id("n"), 0, NodeBudget(llm_calls=5), run.sandbox.base, "")
    from ticketzero.prompts import GATE_SYSTEM
    res = LLM.chat(nc, "gate", [{"role": "system", "content": GATE_SYSTEM}, {"role": "user", "content": "x"}],
                   thinking=False)
    assert '"enough"' in res.content
    assert run.calls_used == 1                       # retries after 429 are not charged again
    limiter = LLM.limiter(res.model)
    assert limiter.blocked_until > 0                 # the whole model was paused
    assert limiter.rpm < limiter.max_rpm             # and slowed down after the 429s


def test_adaptive_limiter_halves_on_429_and_recovers():
    from ticketzero.llm import MIN_RPM, TokenBucket
    b = TokenBucket(24)
    assert b.on_rate_limited() == 60 / 12 and b.rpm == 12
    for _ in range(10):
        b.on_rate_limited()
    assert b.rpm == MIN_RPM                          # never below the floor
    for _ in range(100):
        b.on_success()
    assert b.rpm == 24                               # recovers, but never above the configured RPM
