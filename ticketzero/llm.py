"""Crusoe Managed Inference client: budgeted, rate-limited, retrying chat calls and JSON-mode calls."""
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import openai
from pydantic import BaseModel

from . import config
from .budget import NodeCtx
from .errors import LLMError, NodeFailed
from .models import REGISTRY
from .schemas import parse_model, schema_text
from .store import store
from .util import new_id, redact, stable_json, trunc


MIN_RPM = 2.0   # floor for the adaptive rate


class TokenBucket:
    """Client-side request limiter, one per model.

    Adaptive (AIMD): starts at the configured RPM, halves the rate on every 429, and creeps back up by 5% of the
    configured RPM per successful call. Crusoe's real per-model limit can be far below the documented one, so the
    limiter converges on what the server actually accepts instead of repeatedly hitting it.
    """

    def __init__(self, rpm: int):
        self.max_rpm = float(max(1, rpm))
        self.rpm = self.max_rpm
        self.tokens = 1.0
        self.updated = time.monotonic()
        self.blocked_until = 0.0
        self.lock = threading.Lock()

    @property
    def rate(self) -> float:          # requests per second
        return self.rpm / 60.0

    @property
    def capacity(self) -> float:      # burst size: about 15 seconds' worth at the current rate
        return max(1.0, self.rpm / 4.0)

    def acquire(self, run=None) -> None:
        while True:
            if run:
                run.check_cancel()
            with self.lock:
                now = time.monotonic()
                if now >= self.blocked_until:
                    self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                    self.updated = now
                    if self.tokens >= 1:
                        self.tokens -= 1
                        return
                    wait = (1 - self.tokens) / self.rate
                else:
                    wait = self.blocked_until - now
            time.sleep(min(max(wait, 0.05), 1.0))

    def on_success(self) -> None:
        with self.lock:
            self.rpm = min(self.max_rpm, self.rpm + self.max_rpm * 0.05)

    def on_rate_limited(self) -> float:
        """Halve the rate. Returns the new interval between requests, in seconds."""
        with self.lock:
            self.rpm = max(MIN_RPM, self.rpm / 2)
            return 60.0 / self.rpm

    def block_until_next_minute(self) -> None:
        self.pause(60 - (time.time() % 60))

    def pause(self, seconds: float) -> None:
        """Hold every caller for this model, e.g. after a 429, so threads back off together instead of
        retrying independently into the same limit."""
        with self.lock:
            self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)
            self.tokens = 0.0


@dataclass
class LLMResult:
    content: str
    reasoning: str
    tool_calls: list[dict]
    finish_reason: str
    model: str


class CrusoeLLM:
    def __init__(self) -> None:
        self._client: Optional[openai.OpenAI] = None
        self.limiters: dict[str, TokenBucket] = {}
        self.inflight: dict[str, threading.Semaphore] = {}   # per-model cap on concurrent requests
        self.lock = threading.Lock()
        self.rejected_ctk: dict[str, set[str]] = {}   # model -> rejected chat_template_kwargs keys ("*" = whole field)
        self.rejected_params: dict[str, set[str]] = {}
        self.node_roles: dict[str, dict[str, str]] = {}

    def client(self) -> openai.OpenAI:
        with self.lock:
            if self._client is None:
                self._client = openai.OpenAI(base_url=config.CRUSOE_BASE_URL, api_key=config.CRUSOE_API_KEY,
                                             max_retries=0, timeout=240)
            return self._client

    def limiter(self, model: str) -> TokenBucket:
        with self.lock:
            if model not in self.limiters:
                self.limiters[model] = TokenBucket(config.RPM_PER_MODEL)
            return self.limiters[model]

    def inflight_slot(self, model: str) -> threading.Semaphore:
        with self.lock:
            if model not in self.inflight:
                self.inflight[model] = threading.Semaphore(config.MAX_INFLIGHT_PER_MODEL)
            return self.inflight[model]

    def _create(self, kwargs: dict) -> tuple[Any, dict]:
        """The only network call. Returns (parsed completion, response headers)."""
        raw = self.client().chat.completions.with_raw_response.create(**kwargs)
        return raw.parse(), dict(raw.headers)

    def _note_role(self, nc: NodeCtx, role: str, model: str) -> None:
        with self.lock:
            roles = self.node_roles.setdefault(nc.node_id, {})
            changed = roles.get(role) != model
            roles[role] = model
            snapshot = dict(roles)
        if changed:
            store().update_node(nc.node_id, model_roles_json=stable_json(snapshot))

    def _request(self, nc: NodeCtx, role: str, model: str, kwargs: dict) -> tuple[Any, int]:
        """One budgeted call with exponential backoff on 429/5xx (1s base, doubling, jitter, cap 60s, 6 tries).
        A 429 pauses the whole model (all threads) for the backoff delay, or Retry-After when the server sends it."""
        run = nc.run
        run.charge(nc.budget)
        limiter = self.limiter(model)
        slot = self.inflight_slot(model)
        for attempt in range(6):
            run.check_cancel()
            limiter.acquire(run)
            t0 = time.monotonic()
            try:
                with slot:
                    resp, headers = self._create(kwargs)
                latency = int((time.monotonic() - t0) * 1000)
                limiter.on_success()
                self._watch_rate_headers(nc, model, limiter, headers)
                return resp, latency
            except (openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError,
                    openai.APITimeoutError) as e:
                status = getattr(e, "status_code", None) or type(e).__name__
                delay = min(60.0, 2.0 ** attempt) + random.uniform(0, 0.5)
                if isinstance(e, openai.RateLimitError):
                    interval = limiter.on_rate_limited()
                    delay = min(60.0, max(delay, _retry_after(e), interval))
                    limiter.pause(delay)
                    store().event(run.id, nc.node_id, "budget",
                                  f"Crusoe rate-limited {model}; slowing to {limiter.rpm:.0f} requests/min "
                                  f"and retrying in {delay:.0f}s")
                store().llm_call(run.id, nc.node_id, role, model, 0, 0, 0,
                                 int((time.monotonic() - t0) * 1000), "",
                                 f"transient {status}: {trunc(_error_message(e), 200)}")
                if attempt == 5:
                    raise LLMError(f"Crusoe kept returning {status} for {model} after 6 tries "
                                   f"({trunc(_error_message(e), 120)})") from e
                end = time.monotonic() + delay
                while time.monotonic() < end:
                    run.check_cancel()
                    time.sleep(max(0.0, min(0.5, end - time.monotonic())))
        raise LLMError("unreachable")

    def _watch_rate_headers(self, nc: NodeCtx, model: str, limiter: TokenBucket, headers: dict) -> None:
        lower = {k.lower(): v for k, v in headers.items()}
        rem = lower.get("x-ratelimit-remaining-requests")
        try:
            if rem is not None and float(rem) < 3:
                limiter.block_until_next_minute()
                store().event(nc.run.id, nc.node_id, "budget",
                              f"{model}: remaining requests {rem}, pausing until next minute",
                              {"remaining_tokens": lower.get("x-ratelimit-remaining-tokens")})
        except ValueError:
            pass

    def chat(self, nc: NodeCtx, role: str, messages: list[dict], *, thinking: bool,
             max_tokens: Optional[int] = None, tools: Optional[list[dict]] = None, json_mode: bool = False,
             model_role: Optional[str] = None) -> LLMResult:
        model = REGISTRY.model_for(model_role or role)
        if not model:
            raise LLMError(f"no Nemotron model resolved for role {role}")
        self._note_role(nc, role, model)
        supported = REGISTRY.supported(model)
        cap = REGISTRY.max_completion(model)
        # Reasoning tokens count against max_tokens: at least 4096 with thinking on, 1024 off.
        if max_tokens is None:
            max_tokens = 4096 if thinking else 1024
        if thinking:
            max_tokens = max(max_tokens, 4096)
        if cap:
            max_tokens = min(max_tokens, cap)
        temperature = 0.2 if role == "gate" else 1.0
        ctk: dict[str, Any] = {"enable_thinking": thinking}
        if role in ("fixer", "investigator"):
            ctk["force_nonempty_content"] = True   # recommended for coding agents on the Nemotron card
        use_json = json_mode and "response_format" in supported
        use_tools = tools if tools and "tools" in supported else None
        length_retried = False
        while True:
            kwargs = self._build_kwargs(model, messages, max_tokens, temperature, ctk, use_json, use_tools)
            try:
                resp, latency = self._request(nc, role, model, kwargs)
            except (openai.BadRequestError, openai.PermissionDeniedError, openai.UnprocessableEntityError) as e:
                if self._drop_rejected_param(nc, role, model, kwargs, e):
                    continue
                raise LLMError(f"{model} returned HTTP {getattr(e, 'status_code', 400)}: "
                               f"{trunc(redact(str(e)), 400)}") from e
            except openai.APIStatusError as e:
                store().llm_call(nc.run.id, nc.node_id, role, model, 0, 0, 0, 0, "",
                                 f"{e.status_code}: {trunc(str(e), 300)}")
                raise LLMError(f"{model} returned HTTP {e.status_code}: {trunc(redact(str(e)), 400)}") from e

            result = self._parse_and_log(nc, role, model, resp, latency)
            if result.finish_reason == "length" and not result.content and not result.tool_calls and not length_retried:
                length_retried = True   # spent the budget reasoning: retry once with double max_tokens
                max_tokens = min(max_tokens * 2, cap) if cap else max_tokens * 2
                continue
            return result

    def _build_kwargs(self, model: str, messages: list[dict], max_tokens: int, temperature: float,
                      ctk: dict, use_json: bool, use_tools: Optional[list[dict]]) -> dict:
        rejected = self.rejected_ctk.get(model, set())
        eff_ctk = {} if "*" in rejected else {k: v for k, v in ctk.items() if k not in rejected}
        kwargs: dict[str, Any] = {
            "model": model, "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "top_p": 0.95,
        }
        if eff_ctk:
            kwargs["extra_body"] = {"chat_template_kwargs": eff_ctk}
        if use_json and "response_format" not in self.rejected_params.get(model, set()):
            kwargs["response_format"] = {"type": "json_object"}
        if use_tools:
            kwargs["tools"] = use_tools
            kwargs["tool_choice"] = "auto"
        return kwargs

    def _drop_rejected_param(self, nc: NodeCtx, role: str, model: str, kwargs: dict, e: Exception) -> bool:
        """On 400/403: drop chat_template_kwargs (force_nonempty_content first), then response_format.
        Returns True when something was dropped and the call should be retried."""
        status = getattr(e, "status_code", 400)
        store().llm_call(nc.run.id, nc.node_id, role, model, 0, 0, 0, 0, "", f"{status}: {trunc(str(e), 300)}")
        eff_ctk = (kwargs.get("extra_body") or {}).get("chat_template_kwargs") or {}
        if eff_ctk and status in (400, 403, 422):
            drop = "force_nonempty_content" if "force_nonempty_content" in eff_ctk else "*"
            with self.lock:
                self.rejected_ctk.setdefault(model, set()).add(drop)
            store().event(nc.run.id, nc.node_id, "budget",
                          f"{model} rejected chat_template_kwargs ({drop}); retrying without it")
            return True
        if "response_format" in kwargs:
            with self.lock:
                self.rejected_params.setdefault(model, set()).add("response_format")
            return True
        return False

    def _parse_and_log(self, nc: NodeCtx, role: str, model: str, resp: Any, latency: int) -> LLMResult:
        choice = resp.choices[0] if resp.choices else None
        m = choice.message.model_dump() if choice is not None else {}
        content = m.get("content") or ""
        reasoning = m.get("reasoning_content") or m.get("reasoning") or ""
        tool_calls = []
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            tool_calls.append({"id": tc.get("id") or new_id("call_"), "type": "function",
                               "function": {"name": fn.get("name", ""), "arguments": fn.get("arguments") or "{}"}})
        finish = (choice.finish_reason if choice is not None else "") or ""
        usage = resp.usage.model_dump() if getattr(resp, "usage", None) else {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        cached = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
        nc.run.add_tokens(pt + ct)
        store().llm_call(nc.run.id, nc.node_id, role, model, pt, ct, cached, latency, finish, None)
        return LLMResult(content, reasoning, tool_calls, finish, model)


LLM = CrusoeLLM()


def _retry_after(e: openai.APIStatusError) -> float:
    try:
        return max(0.0, float(e.response.headers.get("retry-after", 0)))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _error_message(e: Exception) -> str:
    """The server's error message when there is one (e.g. "Too many requests. Reason: max_requests_limit")."""
    body = getattr(e, "body", None)
    err = body.get("error", body) if isinstance(body, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    return str(e)


def json_call(nc: NodeCtx, role: str, messages: list[dict], schema: type[BaseModel], *, thinking: bool,
              max_tokens: Optional[int] = None, model_role: Optional[str] = None,
              fallback: Optional[Callable[[str], Optional[BaseModel]]] = None) -> BaseModel:
    """JSON-mode call with one repair call. A node that still cannot get valid JSON fails with that reason."""
    res = LLM.chat(nc, role, messages, thinking=thinking, max_tokens=max_tokens, json_mode=True, model_role=model_role)
    obj, err = parse_model(schema, res.content)
    if obj is None and fallback:
        obj = fallback(res.content)
    if obj is not None:
        return obj
    repair = messages + [
        {"role": "assistant", "content": trunc(res.content or "(empty response)", 6000)},
        {"role": "user", "content": "Return only valid JSON matching this schema: " + schema_text(schema)},
    ]
    res2 = LLM.chat(nc, role, repair, thinking=False, max_tokens=max(max_tokens or 0, 4096), json_mode=True,
                    model_role=model_role)
    obj, err2 = parse_model(schema, res2.content)
    if obj is None and fallback:
        obj = fallback(res2.content)
    if obj is None:
        raise NodeFailed(f"{role} returned invalid JSON after one repair call: {err2 or err}")
    return obj
