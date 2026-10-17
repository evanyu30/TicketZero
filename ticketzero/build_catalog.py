#!/usr/bin/env python3
"""
Builds the TicketZero benchmark: 10 broken "customer" tickets.

How it works
------------
There is ONE correct customer app (GOLDEN_APP below, "Fog City Concierge").
Each ticket = that app with exactly one bug injected, plus the customer's
complaint (ticket.md) and the same frozen acceptance tests (test_app.py).
Two tickets have NO code bug: they are platform-side outages, and the correct
outcome is ESCALATED, not a patch.

Usage
-----
  pip install -r requirements.txt
  python build_catalog.py --model <WORKING_MODEL_ID>
  python build_catalog.py --model <WORKING_MODEL_ID> --validate   # needs CRUSOE_API_KEY

Pick <WORKING_MODEL_ID> from GET https://api.inference.crusoecloud.com/v1/models:
it must list "tools" in supported_parameters, and a non-reasoning instruct
model is safest.

Everything here is synthetic test data written for this hackathon.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
TICKETS_DIR = ROOT / "tickets"
KEY_DIR = ROOT / "_answer_key"

# ---------------------------------------------------------------------------
# The correct customer app. Every ticket is this file plus one bug.
# ---------------------------------------------------------------------------
GOLDEN_APP = '''"""Fog City Concierge: a small customer app built on Crusoe Managed Inference.

ask(question)       -> answers weather questions by calling the get_weather tool
weather_card(city)  -> returns a JSON weather card: {"city", "temp_c", "advice"}
"""
import json
import os

from openai import OpenAI

BASE_URL = "https://api.inference.crusoecloud.com/v1"
API_KEY_ENV = "CRUSOE_API_KEY"
MODEL = "__MODEL__"
MAX_TOKENS = 1024
CARD_MAX_TOKENS = 1024

client = OpenAI(base_url=BASE_URL, api_key=os.environ.get(API_KEY_ENV))

TOOL_CALLS = []  # every city get_weather was called with (the tests read this)


def get_weather(city):
    """Fake weather service. Returns a fixed reading no model could guess."""
    TOOL_CALLS.append(city)
    return {"city": city, "temp_c": 13.7, "conditions": "fog"}


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]

SYSTEM = (
    "You are a city concierge. For any weather question, call get_weather. "
    "Always state the exact temperature the tool returns, in Celsius."
)


def ask(question):
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": question},
    ]
    for _ in range(3):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            max_tokens=MAX_TOKENS,
        )
        msg = response.choices[0].message
        if not msg.tool_calls:
            return msg.content or ""
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
            }
        )
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            result = get_weather(**args)
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)}
            )
    return ""


def _extract_json(text):
    """Pull the JSON object out of a reply, even if wrapped in ``` fences."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object in model reply: {text!r}")
    return json.loads(text[start : end + 1])


def weather_card(city):
    weather = get_weather(city)
    prompt = (
        "Return ONLY a JSON object with keys city (string), temp_c (number), "
        "advice (one short sentence). Use this data: " + json.dumps(weather)
    )
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=CARD_MAX_TOKENS,
    )
    return _extract_json(response.choices[0].message.content or "")
'''

# ---------------------------------------------------------------------------
# Frozen acceptance tests. Identical for every ticket. The Repair agent may
# never edit this file; the orchestrator should hash it before and after.
# ---------------------------------------------------------------------------
FROZEN_TESTS = '''"""Frozen acceptance tests. TicketZero's Repair agent must NOT edit this file."""
from app import TOOL_CALLS, ask, weather_card


def test_ask_uses_weather_tool():
    TOOL_CALLS.clear()
    reply = ask("What's the weather in San Francisco right now?")
    assert TOOL_CALLS, "get_weather was never called"
    assert "13.7" in reply, f"Reply did not report the tool's temperature: {reply!r}"


def test_weather_card_is_valid_json():
    card = weather_card("San Francisco")
    assert set(card) >= {"city", "temp_c", "advice"}, f"Missing keys: {card}"
    assert float(card["temp_c"]) == 13.7, f"Wrong temperature: {card}"
'''

LOG_503 = """2026-10-16T02:09:51Z INFO  ask() request_id=req_6c11d0 POST /v1/chat/completions -> 200 OK (412 ms)
2026-10-16T02:11:04Z ERROR ask() request_id=req_7f3a91 POST /v1/chat/completions -> 503 Service Unavailable {"error":{"message":"upstream model server unavailable","type":"server_error"}}
2026-10-16T02:14:37Z ERROR weather_card() request_id=req_7f4e02 POST /v1/chat/completions -> 503 Service Unavailable {"error":{"message":"upstream model server unavailable","type":"server_error"}}
2026-10-16T02:22:10Z ERROR ask() request_id=req_7fa9b3 POST /v1/chat/completions -> 503 Service Unavailable {"error":{"message":"upstream model server unavailable","type":"server_error"}}
2026-10-16T02:35:48Z ERROR ask() request_id=req_80d117 POST /v1/chat/completions -> 503 Service Unavailable {"error":{"message":"upstream model server unavailable","type":"server_error"}}
2026-10-16T02:41:18Z INFO  ask() request_id=req_8b02cc POST /v1/chat/completions -> 200 OK (398 ms)
2026-10-16T02:43:02Z INFO  weather_card() request_id=req_8b1f47 POST /v1/chat/completions -> 200 OK (377 ms)
"""

LOG_500 = """2026-10-16T14:02:11Z INFO  ask() request_id=req_a10233 POST /v1/chat/completions -> 200 OK (405 ms)
2026-10-16T14:02:19Z INFO  ask() request_id=req_a10298 POST /v1/chat/completions -> 200 OK (391 ms)
2026-10-16T14:03:40Z ERROR ask() request_id=req_a1077e POST /v1/chat/completions -> 500 Internal Server Error {"error":{"message":"internal error","type":"server_error"}}
2026-10-16T14:03:44Z INFO  ask() request_id=req_a10781 POST /v1/chat/completions -> 200 OK (402 ms)  [retry of req_a1077e]
2026-10-16T16:20:05Z INFO  weather_card() request_id=req_b3301c POST /v1/chat/completions -> 200 OK (366 ms)
2026-10-16T16:21:57Z ERROR weather_card() request_id=req_b330f9 POST /v1/chat/completions -> 500 Internal Server Error {"error":{"message":"internal error","type":"server_error"}}
2026-10-16T16:22:01Z INFO  weather_card() request_id=req_b33102 POST /v1/chat/completions -> 200 OK (371 ms)  [retry of req_b330f9]
2026-10-17T09:15:33Z ERROR ask() request_id=req_c9e440 POST /v1/chat/completions -> 500 Internal Server Error {"error":{"message":"internal error","type":"server_error"}}
2026-10-17T09:15:37Z INFO  ask() request_id=req_c9e452 POST /v1/chat/completions -> 200 OK (399 ms)  [retry of req_c9e440]
"""

# ---------------------------------------------------------------------------
# The 10 tickets. "bug" = list of (old_text, new_text) applied to GOLDEN_APP.
# ---------------------------------------------------------------------------
TICKETS = [
    {
        "id": "T01",
        "slug": "retired_model",
        "split": "dev",
        "category": "Model lifecycle",
        "bug": [('MODEL = "__MODEL__"', 'MODEL = "__RETIRED_MODEL__"')],
        "ticket": (
            "Subject: Concierge app completely down\n\n"
            "Hi Crusoe team. Our Fog City Concierge app worked fine last month and we haven't "
            "touched the code. This week every single request fails before we get any answer; "
            "both ask() and weather_card() error out. We need it back up today.\n\n"
            "Expected: ask(\"What's the weather in San Francisco?\") reports the temperature from "
            "our get_weather tool."
        ),
        "expected_status": "VERIFIED",
        "root_cause": "The hard-coded MODEL id was retired from Crusoe Managed Inference; the API rejects it.",
        "reference_fix": "Change MODEL to a current model from GET /v1/models whose supported_parameters include 'tools'.",
    },
    {
        "id": "T02",
        "slug": "wrong_base_url",
        "split": "dev",
        "category": "Configuration",
        "bug": [
            (
                'BASE_URL = "https://api.inference.crusoecloud.com/v1"',
                'BASE_URL = "https://api.crusoecloud.com/v1"',
            )
        ],
        "ticket": (
            "Subject: Can't connect after moving to Crusoe\n\n"
            "We just moved this app to Crusoe, copying the settings from an old internal wiki page. "
            "Every call fails with a connection / not-found style error. Our API key is valid and "
            "shows up in the Crusoe console."
        ),
        "expected_status": "VERIFIED",
        "root_cause": "BASE_URL points at the wrong host; Managed Inference lives at api.inference.crusoecloud.com/v1.",
        "reference_fix": "Set BASE_URL = 'https://api.inference.crusoecloud.com/v1'.",
    },
    {
        "id": "T03",
        "slug": "tool_call_ignored",
        "split": "dev",
        "category": "Function calling",
        "bug": [
            (
                '        if not msg.tool_calls:\n            return msg.content or ""\n',
                '        return msg.content or ""  # the model always answers directly\n',
            )
        ],
        "ticket": (
            "Subject: Blank answers since switching models\n\n"
            "Since we switched to a newer model, ask() returns an empty string for weather "
            "questions. No error at all, just blank. weather_card() still works. Our old model "
            "used to answer these directly."
        ),
        "expected_status": "VERIFIED",
        "root_cause": "The code returns message.content immediately and never handles message.tool_calls; the new model replies with a tool call and empty content.",
        "reference_fix": "Only return content when there are no tool_calls; otherwise run the tool and send the result back.",
    },
    {
        "id": "T04",
        "slug": "tool_args_not_parsed",
        "split": "held_out",
        "category": "Function calling",
        "bug": [
            (
                "args = json.loads(tc.function.arguments)",
                "args = tc.function.arguments",
            )
        ],
        "ticket": (
            "Subject: TypeError in tool handling\n\n"
            "ask() now crashes with: TypeError: get_weather() argument after ** must be a "
            "mapping, not str. A contractor 'simplified' our tool-handling code last week. "
            "weather_card() is fine."
        ),
        "expected_status": "VERIFIED",
        "root_cause": "tool_call.function.arguments is a JSON string; the code passes it to get_weather(**args) without json.loads.",
        "reference_fix": "args = json.loads(tc.function.arguments).",
    },
    {
        "id": "T05",
        "slug": "json_truncated",
        "split": "dev",
        "category": "Structured output",
        "bug": [("CARD_MAX_TOKENS = 1024", "CARD_MAX_TOKENS = 12")],
        "ticket": (
            "Subject: weather_card JSON broken\n\n"
            "Finance asked us to cut token spend, so we tightened some limits. Now weather_card() "
            "throws a JSON decode error every time. ask() still works."
        ),
        "expected_status": "VERIFIED",
        "root_cause": "CARD_MAX_TOKENS is too small; the reply is cut off mid-JSON (finish_reason='length').",
        "reference_fix": "Raise CARD_MAX_TOKENS (e.g. back to 1024).",
    },
    {
        "id": "T06",
        "slug": "max_tokens_too_large",
        "split": "held_out",
        "category": "Request parameters",
        "bug": [("\nMAX_TOKENS = 1024\n", "\nMAX_TOKENS = 4_000_000\n")],
        "ticket": (
            "Subject: 400 Bad Request on every ask()\n\n"
            "We raised max_tokens a lot so long answers never get cut off. Now ask() fails with a "
            "400 Bad Request every time. weather_card() works."
        ),
        "expected_status": "VERIFIED",
        "root_cause": "MAX_TOKENS exceeds the model's context window, so the API rejects the request.",
        "reference_fix": "Set MAX_TOKENS within the model's context_length from /v1/models (e.g. 1024).",
    },
    {
        "id": "T07",
        "slug": "platform_outage_503",
        "split": "dev",
        "category": "Platform incident (escalate)",
        "bug": [],
        "extra_files": {"logs.txt": LOG_503},
        "ticket": (
            "Subject: Everything failed last night, is it our code?\n\n"
            "Between about 02:10 and 02:40 UTC last night our concierge returned errors for every "
            "request. We didn't deploy anything. Logs attached (logs.txt). Can you fix our code?"
        ),
        "expected_status": "ESCALATED",
        "root_cause": "No customer-code bug. The logs show a window of 503 server_error responses that recovered on its own: a platform-side incident.",
        "reference_fix": "Do not patch. Reproduce (it works now), then escalate with the time window and request IDs (e.g. req_7f3a91).",
    },
    {
        "id": "T08",
        "slug": "wrong_api_key_env",
        "split": "dev",
        "category": "Migration",
        "bug": [('API_KEY_ENV = "CRUSOE_API_KEY"', 'API_KEY_ENV = "OPENAI_API_KEY"')],
        "ticket": (
            "Subject: Auth failing after migration\n\n"
            "We migrated from OpenAI last week. CRUSOE_API_KEY is set in our environment and works "
            "with curl, but the app fails on startup or with authentication errors."
        ),
        "expected_status": "VERIFIED",
        "root_cause": "The client reads the API key from OPENAI_API_KEY (left over from the old provider) instead of CRUSOE_API_KEY.",
        "reference_fix": "API_KEY_ENV = 'CRUSOE_API_KEY'.",
    },
    {
        "id": "T09",
        "slug": "stream_misread",
        "split": "held_out",
        "category": "Streaming",
        "bug": [
            (
                "            tools=TOOLS,\n            max_tokens=MAX_TOKENS,\n",
                "            tools=TOOLS,\n            max_tokens=MAX_TOKENS,\n            stream=True,\n",
            )
        ],
        "ticket": (
            "Subject: Crash after enabling streaming\n\n"
            "We turned on streaming to make our UI feel faster. Now ask() crashes with "
            "AttributeError: 'Stream' object has no attribute 'choices'."
        ),
        "expected_status": "VERIFIED",
        "root_cause": "stream=True returns a Stream of chunks, but the code reads it like a normal response.",
        "reference_fix": "Remove stream=True from ask(), or consume the stream and rebuild the message (incl. tool_calls).",
    },
    {
        "id": "T10",
        "slug": "intermittent_500",
        "split": "held_out",
        "category": "Platform incident (escalate)",
        "bug": [],
        "extra_files": {"logs.txt": LOG_500},
        "ticket": (
            "Subject: Random 500 errors since yesterday\n\n"
            "Roughly 1 in 10 requests fail with HTTP 500 since yesterday. Retrying usually works. "
            "Request IDs are in the attached logs.txt. Can you fix our code?"
        ),
        "expected_status": "ESCALATED",
        "root_cause": "No customer-code bug. Intermittent 500 server_error responses that succeed on retry point to the platform.",
        "reference_fix": "Escalate with request IDs (e.g. req_a1077e, req_b330f9, req_c9e440). A retry/backoff patch is acceptable as a mitigation only if it is also escalated.",
    },
]

REQUIREMENTS = "openai>=1.40\npytest>=8\n"


def apply_bug(ticket):
    text = GOLDEN_APP
    for old, new in ticket["bug"]:
        count = text.count(old)
        if count != 1:
            sys.exit(f"{ticket['id']}: expected to find bug target exactly once, found {count}: {old!r}")
        text = text.replace(old, new)
    return text


def build(model, retired_model):
    if TICKETS_DIR.exists():
        shutil.rmtree(TICKETS_DIR)
    if KEY_DIR.exists():
        shutil.rmtree(KEY_DIR)
    TICKETS_DIR.mkdir()
    KEY_DIR.mkdir()

    (ROOT / "requirements.txt").write_text(REQUIREMENTS)

    answers = []
    for t in TICKETS:
        folder = TICKETS_DIR / f"{t['id']}_{t['slug']}"
        folder.mkdir()
        app = apply_bug(t).replace("__RETIRED_MODEL__", retired_model).replace("__MODEL__", model)
        (folder / "app.py").write_text(app)
        (folder / "test_app.py").write_text(FROZEN_TESTS)
        (folder / "ticket.md").write_text(f"# Ticket {t['id']}\n\n{t['ticket']}\n")
        for name, content in t.get("extra_files", {}).items():
            (folder / name).write_text(content)
        answers.append(
            {
                "id": t["id"],
                "folder": folder.name,
                "split": t["split"],
                "category": t["category"],
                "expected_status": t["expected_status"],
                "root_cause": t["root_cause"],
                "reference_fix": t["reference_fix"],
            }
        )

    (KEY_DIR / "answers.json").write_text(json.dumps(answers, indent=2) + "\n")
    (KEY_DIR / "golden_app.py").write_text(GOLDEN_APP.replace("__MODEL__", model))
    print(f"Built {len(TICKETS)} tickets in {TICKETS_DIR}/ and the answer key in {KEY_DIR}/")


def run_pytest(folder):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(folder)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def validate():
    """Live check against Crusoe: golden app must PASS, code-bug tickets must FAIL."""
    if not os.environ.get("CRUSOE_API_KEY"):
        sys.exit("Set CRUSOE_API_KEY first.")
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(KEY_DIR / "golden_app.py", Path(tmp) / "app.py")
        (Path(tmp) / "test_app.py").write_text(FROZEN_TESTS)
        golden_pass = run_pytest(tmp)
    print(f"{'GOLDEN':<28} tests {'PASS' if golden_pass else 'FAIL'}   want PASS   {'ok' if golden_pass else 'PROBLEM: pick another model'}")
    ok &= golden_pass
    for t in TICKETS:
        folder = TICKETS_DIR / f"{t['id']}_{t['slug']}"
        passed = run_pytest(folder)
        want_pass = t["expected_status"] == "ESCALATED"  # escalation tickets have no code bug
        good = passed == want_pass
        ok &= good
        print(
            f"{folder.name:<28} tests {'PASS' if passed else 'FAIL'}   want {'PASS' if want_pass else 'FAIL'}   "
            f"{'ok' if good else 'PROBLEM'}"
        )
    print("\nAll good." if ok else "\nSome tickets need attention (see PROBLEM rows).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="PUT-WORKING-MODEL-ID-HERE", help="current model id with tool support")
    parser.add_argument(
        "--retired-model",
        default="moonshotai/Kimi-K2.6",
        help="a model id that is no longer served (confirm it's absent from /v1/models)",
    )
    parser.add_argument("--validate", action="store_true", help="run the tests live against Crusoe")
    args = parser.parse_args()
    build(args.model, args.retired_model)
    if args.validate:
        validate()
