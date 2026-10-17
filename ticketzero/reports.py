"""Usage accounting, execution tree views, and the final report / escalation bundle."""
import re

from .budget import NodeBudget, NodeCtx
from .errors import BudgetExhausted, LLMError, NodeFailed, RunCancelled
from .llm import json_call
from .models import estimate_cost
from .prompts import ESCALATION_SYSTEM, ticket_block
from .schemas import NextStepsOut
from .store import store
from .tasks import NodeResult
from .util import now_iso, redact, trunc


def usage_summary(run_id: str) -> dict:
    """Calls, tokens, cost estimate, prefix-cache hit rate, and a per-model/role breakdown."""
    rows = store().query("SELECT * FROM llm_calls WHERE run_id=?", (run_id,))
    ok = [r for r in rows if not r["error"]]
    charged = [r for r in rows if not (r["error"] or "").startswith("transient")]   # 429/5xx retries are free
    prompt = sum(r["prompt_tokens"] or 0 for r in ok)
    completion = sum(r["completion_tokens"] or 0 for r in ok)
    cached = sum(r["cached_tokens"] or 0 for r in ok)
    cost = sum(estimate_cost(r["model"], r["prompt_tokens"] or 0, r["completion_tokens"] or 0, r["cached_tokens"] or 0)
               for r in ok)
    by_key: dict[tuple, dict] = {}
    for r in rows:
        d = by_key.setdefault((r["model"], r["role"]), {
            "model": r["model"], "role": r["role"], "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "cached_tokens": 0, "latency_ms_total": 0, "errors": 0, "errors_429": 0, "errors_5xx": 0})
        if r["error"]:
            d["errors"] += 1
            if "429" in r["error"] or "RateLimit" in r["error"]:
                d["errors_429"] += 1
            if re.search(r"\b5\d\d\b|InternalServer", r["error"]):
                d["errors_5xx"] += 1
            continue
        d["calls"] += 1
        d["prompt_tokens"] += r["prompt_tokens"] or 0
        d["completion_tokens"] += r["completion_tokens"] or 0
        d["cached_tokens"] += r["cached_tokens"] or 0
        d["latency_ms_total"] += r["latency_ms"] or 0
    per_model = []
    for d in by_key.values():
        d["avg_latency_ms"] = int(d.pop("latency_ms_total") / d["calls"]) if d["calls"] else 0
        per_model.append(d)
    return {"llm_calls": len(charged), "successful_calls": len(ok), "prompt_tokens": prompt,
            "completion_tokens": completion, "cached_tokens": cached, "total_tokens": prompt + completion,
            "cost_usd": round(cost, 5), "cache_hit_rate": round(cached / prompt, 4) if prompt else 0.0,
            "per_model": sorted(per_model, key=lambda d: (d["model"] or "", d["role"] or ""))}


def build_tree(run_id: str) -> list[dict]:
    rows = store().query("SELECT id, parent_id, depth, title, status, reason, kind, replans FROM nodes "
                         "WHERE run_id=? ORDER BY created_at, rowid", (run_id,))
    by_id = {r["id"]: dict(r, children=[]) for r in rows}
    roots = []
    for r in rows:
        node = by_id[r["id"]]
        if r["parent_id"] and r["parent_id"] in by_id:
            by_id[r["parent_id"]]["children"].append(node)
        else:
            roots.append(node)
    return roots


def tree_outline(run_id: str) -> str:
    lines: list[str] = []

    def walk(n: dict, indent: int) -> None:
        lines.append(f"{'  ' * indent}- [{n['status']}] depth {n['depth']}: {n['title']}"
                     + (f" ({trunc(n['reason'], 160)})" if n.get("reason") else ""))
        for c in n["children"]:
            walk(c, indent + 1)

    for root in build_tree(run_id):
        walk(root, 0)
    return "\n".join(lines)


def criteria_status(criteria: list[dict], results: dict[str, str]) -> list[dict]:
    out = []
    for c in criteria:
        states = [results.get(t, "not run") for t in c["test_ids"]]
        if any(s in ("failed", "error", "skipped") for s in states):
            status = "fail"
        elif states and all(s == "passed" for s in states):
            status = "pass"
        else:
            status = "not run"
        out.append(dict(c, status=status, tests={t: results.get(t, "not run") for t in c["test_ids"]}))
    return out


def proof_md(proof) -> str:
    if not proof:
        return "_no verifier output_\n"
    tests = proof.get("tests") or {}
    critic = proof.get("critic") or {}
    lines = [f"- Frozen test hash: {'ok' if proof.get('hash_ok') else 'MISMATCH'} (`{proof.get('frozen_hash')}`)",
             f"- Scope: {proof.get('scope')}",
             f"- Command: `{tests.get('cmd', '')}` (exit code {tests.get('exit_code')}, live tests "
             f"{'enabled' if tests.get('live_tests_enabled') else 'disabled'})",
             f"- Result: {proof.get('summary', '')}"]
    for tid, outcome in sorted((tests.get("results") or {}).items()):
        lines.append(f"  - `{tid}`: {outcome}")
    if critic:
        lines.append(f"- Critic ({critic.get('model')}): veto={critic.get('veto')} - {critic.get('reason', '')}")
    lines.append("\nTest output tail:\n\n```text\n" + (tests.get("output_tail") or "") + "\n```\n")
    return "\n".join(lines) + "\n"


def citations_md(run_id: str) -> str:
    rows = store().query("SELECT url, title, retrieved_at FROM citations WHERE run_id=? ORDER BY id", (run_id,))
    seen, lines = set(), []
    for r in rows:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        lines.append(f"- [{r['title'] or r['url']}]({r['url']}) (retrieved {r['retrieved_at']})")
    return "\n".join(lines) or "_none_"


def write_report(run, result: NodeResult, crit: list[dict], next_checks: list[str]) -> str:
    t = run.ticket
    u = usage_summary(run.id)
    status = {"verified": "VERIFIED", "cancelled": "CANCELLED"}.get(result.status, "ESCALATED")
    md = [f"# TicketZero report: {status}", "",
          f"- Run: `{run.id}` (scenario: {run.scenario})",
          f"- Finished: {now_iso()}",
          f"- Outcome reason: {result.reason}",
          f"- LLM calls: {u['llm_calls']}/{run.budget.max_llm_calls}, tokens: {u['total_tokens']}, "
          f"est. cost: ${u['cost_usd']:.4f}, prefix-cache hit rate: {u['cache_hit_rate']:.1%}", "",
          "## Ticket", f"**{t.get('title', '')}**", "", t.get("description", ""), "",
          f"Expected behavior: {t.get('expected_behavior', '')}", "",
          "## Acceptance criteria", "| ID | Criterion | Tests | Final status |", "|---|---|---|---|"]
    for c in crit:
        md.append(f"| {c['id']} | {c['text']} | {', '.join(f'`{x}`' for x in c['test_ids'])} | {c['status']} |")
    if result.status == "verified":
        md += ["", "## Verified patch", "```diff", result.patch.rstrip(), "```", "", "## Proof", proof_md(result.proof)]
    md += ["", "## Execution tree", tree_outline(run.id)]
    if result.status != "verified":
        neg = store().query("SELECT f.summary, n.title FROM findings f JOIN nodes n ON f.node_id=n.id "
                            "WHERE f.run_id=? AND f.polarity='negative' ORDER BY f.id", (run.id,))
        md += ["", "## Negative findings"]
        md += [f"- [{r['title']}] {trunc(r['summary'], 400)}" for r in neg] or ["_none_"]
        md += ["", "## Best patch attempt"]
        best = run.best_attempt
        if best:
            md += ["```diff", best["patch"].rstrip(), "```", "", "### Verifier output", proof_md(best["proof"])]
        else:
            md.append("_no patch reached verification_")
        md += ["", "## What a human should check next"] + [f"{i}. {c}" for i, c in enumerate(next_checks, 1)]
    md += ["", "## Citations", citations_md(run.id)]
    return redact("\n".join(md) + "\n")


def escalation_checks(run, result: NodeResult) -> list[str]:
    """Super writes the human checklist; falls back to a deterministic list when the run budget is spent."""
    neg = store().query("SELECT summary FROM findings WHERE run_id=? AND polarity='negative' ORDER BY id", (run.id,))
    bundle = (ticket_block(run) + "\n# Outcome\n" + result.reason + "\n\n# Execution tree\n" + tree_outline(run.id)
              + "\n\n# Negative findings\n" + "\n".join(f"- {trunc(r['summary'], 300)}" for r in neg[-25:])
              + "\n\n# Citations\n" + citations_md(run.id))
    nc = NodeCtx(run, run.root_id, 0, NodeBudget(llm_calls=1), run.sandbox.base, run.sandbox.base_commit)
    try:
        out = json_call(nc, "planner", [{"role": "system", "content": ESCALATION_SYSTEM},
                                        {"role": "user", "content": bundle}], NextStepsOut, thinking=False,
                        max_tokens=2048)
        if out.checks:
            return out.checks[:7]
    except (BudgetExhausted, NodeFailed, LLMError, RunCancelled):
        pass
    checks = ["Review the negative findings and the best patch attempt below; confirm which frozen tests still fail."]
    if any("insufficient evidence" in (r["summary"] or "") for r in neg):
        checks.append("No documented behavior was found for part of this ticket; contact Crusoe support to confirm "
                      "the supported request format and model availability.")
    if run.exhausted():
        checks.append(f"The run stopped on its budget ({run.exhausted_reason()}); rerun with a larger budget if the "
                      "evidence suggests a fix is close.")
    checks.append("Check the live /v1/models catalog for this project and the Crusoe changelog for deprecations.")
    return checks
