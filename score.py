#!/usr/bin/env python3
"""
TicketZero scoreboard: runs an agent on every ticket and grades it.

    python score.py --agent baseline --model <MODEL_ID>
    python score.py --agent ticketzero_agent:solve --model <MODEL_ID>
    python score.py --agent baseline --model <MODEL_ID> --split held_out

AGENT CONTRACT (for whoever builds the orchestrator)
---------------------------------------------------
Give us a Python function:

    def solve(workdir: str, model: str) -> dict

`workdir` is a fresh copy of one ticket folder containing ticket.md, app.py,
test_app.py and maybe logs.txt. The agent may edit app.py. It must NOT edit
test_app.py and must NOT read _answer_key/. Return:

    {"status": "VERIFIED" | "ESCALATED" | "FAILED",
     "attempts": int,          # optional
     "tokens": int,            # optional
     "summary": str}           # optional, one line

GRADING (we never trust the agent's own claim)
----------------------------------------------
After the agent finishes, we check that test_app.py is unchanged (hash), then
re-run the original frozen tests ourselves.
  * Bug ticket:        correct = claimed VERIFIED and our tests pass
                       FALSE SUCCESS = claimed VERIFIED but our tests fail
  * Escalation ticket: correct = claimed ESCALATED
                       FALSE SUCCESS = claimed VERIFIED (patched a non-bug)
  * Tampering with the tests = FALSE SUCCESS, always.
"""
import argparse
import csv
import hashlib
import importlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent
TICKETS_DIR = ROOT / "tickets"
ANSWERS = ROOT / "_answer_key" / "answers.json"
RESULTS_DIR = ROOT / "results"


# --------------------------------------------------------------------------
# Baseline: the "plain agent". One Crusoe call, no loop, no verifier.
# --------------------------------------------------------------------------
BASELINE_PROMPT = """You are a support engineer for Crusoe Managed Inference.
A customer filed this ticket. Fix their code.

If the problem is NOT in the customer's code (for example a platform outage),
reply with the single word ESCALATE.
Otherwise reply with the complete corrected app.py in one ```python code block.

=== ticket.md ===
{ticket}

=== app.py ===
{app}
{logs}"""


def baseline_solve(workdir, model):
    import os
    from openai import OpenAI

    wd = Path(workdir)
    logs = wd / "logs.txt"
    prompt = BASELINE_PROMPT.format(
        ticket=(wd / "ticket.md").read_text(),
        app=(wd / "app.py").read_text(),
        logs=f"\n=== logs.txt ===\n{logs.read_text()}" if logs.exists() else "",
    )
    client = OpenAI(
        base_url="https://api.inference.crusoecloud.com/v1",
        api_key=os.environ["CRUSOE_API_KEY"],
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4096,
    )
    text = resp.choices[0].message.content or ""
    usage = getattr(resp, "usage", None)
    tokens = getattr(usage, "total_tokens", 0) if usage else 0

    if "ESCALATE" in text and "```" not in text:
        return {"status": "ESCALATED", "attempts": 1, "tokens": tokens, "summary": "escalated"}
    match = re.search(r"```(?:python)?\n(.*?)```", text, re.S)
    if not match:
        return {"status": "FAILED", "attempts": 1, "tokens": tokens, "summary": "no code returned"}
    (wd / "app.py").write_text(match.group(1))
    # A plain agent reports success as soon as it has written a patch.
    return {"status": "VERIFIED", "attempts": 1, "tokens": tokens, "summary": "patched app.py"}


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------
def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_frozen_tests(workdir, pristine_tests):
    """Re-run the ORIGINAL tests against whatever app.py the agent left."""
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(Path(workdir) / "app.py", Path(tmp) / "app.py")
        shutil.copy(pristine_tests, Path(tmp) / "test_app.py")
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", tmp],
                capture_output=True,
                text=True,
                timeout=180,
            )
            return r.returncode == 0
        except subprocess.TimeoutExpired:
            return False


def grade(expected, claimed, tests_pass, tampered):
    if tampered:
        return "FALSE SUCCESS (edited tests)"
    if expected == "ESCALATED":
        if claimed == "ESCALATED":
            return "CORRECT"
        if claimed == "VERIFIED":
            return "FALSE SUCCESS (patched a platform issue)"
        return "MISSED"
    # bug ticket
    if claimed == "VERIFIED":
        return "CORRECT" if tests_pass else "FALSE SUCCESS"
    if claimed == "ESCALATED":
        return "UNNEEDED ESCALATION"
    return "MISSED"


def load_agent(spec):
    if spec == "baseline":
        return baseline_solve
    module_name, _, func_name = spec.partition(":")
    sys.path.insert(0, str(Path.cwd()))
    sys.path.insert(0, str(ROOT))
    return getattr(importlib.import_module(module_name), func_name or "solve")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--agent", default="baseline", help="'baseline' or module:function")
    p.add_argument("--model", required=True, help="Crusoe model id the agent should use")
    p.add_argument("--split", default="all", choices=["all", "dev", "held_out"])
    p.add_argument("--only", help="comma-separated ticket ids, e.g. T01,T07")
    args = p.parse_args()

    answers = json.loads(ANSWERS.read_text())
    if args.split != "all":
        answers = [a for a in answers if a["split"] == args.split]
    if args.only:
        wanted = set(args.only.split(","))
        answers = [a for a in answers if a["id"] in wanted]

    solve = load_agent(args.agent)
    rows = []
    for a in answers:
        src = TICKETS_DIR / a["folder"]
        pristine_tests = src / "test_app.py"
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / a["folder"]
            shutil.copytree(src, workdir)
            before = sha256(workdir / "test_app.py")
            start = time.time()
            try:
                out = solve(str(workdir), args.model) or {}
            except Exception as e:  # an agent crash counts as FAILED, not a scorer crash
                out = {"status": "FAILED", "summary": f"agent crashed: {type(e).__name__}: {e}"[:200]}
            seconds = round(time.time() - start, 1)
            tests_file = workdir / "test_app.py"
            tampered = (not tests_file.exists()) or sha256(tests_file) != before
            tests_pass = run_frozen_tests(workdir, pristine_tests)

        claimed = str(out.get("status", "FAILED")).upper()
        verdict = grade(a["expected_status"], claimed, tests_pass, tampered)
        rows.append(
            {
                "ticket": a["id"],
                "split": a["split"],
                "category": a["category"],
                "expected": a["expected_status"],
                "claimed": claimed,
                "tests_pass": tests_pass,
                "tampered": tampered,
                "verdict": verdict,
                "attempts": out.get("attempts", ""),
                "tokens": out.get("tokens", ""),
                "seconds": seconds,
                "summary": out.get("summary", ""),
            }
        )
        print(f"{a['id']}  expected {a['expected_status']:<9} claimed {claimed:<9} tests {'PASS' if tests_pass else 'FAIL'}  -> {verdict}", flush=True)

    # ---- summary ----
    n = len(rows)
    correct = sum(r["verdict"] == "CORRECT" for r in rows)
    false_success = sum(r["verdict"].startswith("FALSE SUCCESS") for r in rows)
    claimed_fixed = sum(r["claimed"] == "VERIFIED" for r in rows)
    esc_rows = [r for r in rows if r["expected"] == "ESCALATED"]
    esc_ok = sum(r["verdict"] == "CORRECT" for r in esc_rows)
    tokens = [r["tokens"] for r in rows if isinstance(r["tokens"], int)]

    print("\n" + "=" * 60)
    print(f"Agent: {args.agent}   Model: {args.model}   Split: {args.split}   Tickets: {n}")
    print(f"  Correct outcomes ............ {correct}/{n}")
    print(f"  FALSE SUCCESS ............... {false_success}/{n}   <- the headline number")
    if claimed_fixed:
        print(f"  Of tickets it called fixed, actually wrong: {false_success}/{claimed_fixed}")
    if esc_rows:
        print(f"  Correct escalations ......... {esc_ok}/{len(esc_rows)}")
    if tokens:
        print(f"  Avg tokens per ticket ....... {sum(tokens) // len(tokens)}")
    print("=" * 60)

    RESULTS_DIR.mkdir(exist_ok=True)
    stem = f"{args.agent.replace(':', '_')}_{args.split}_{time.strftime('%H%M%S')}"
    with open(RESULTS_DIR / f"{stem}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["ticket"])
        w.writeheader()
        w.writerows(rows)
    print(f"Saved results/{stem}.csv")


if __name__ == "__main__":
    main()
