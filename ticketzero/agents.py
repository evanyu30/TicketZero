"""Role calls: each function is one Nemotron role from the routing table (F7).

  nano_triage, nano_evidence_gate          -> gate (Nano, thinking off)
  planner_define_success, planner_decompose -> planner (Super, or Ultra at depth 0 when listed)
  investigate                               -> investigator (Lightning tool loop)
  propose_fix                               -> fixer (Lightning)
The Independent Verifier critic lives in verify.py.
"""
import json
import re
from pathlib import Path
from typing import Optional

from .budget import NodeCtx
from .llm import LLM, json_call
from .models import REGISTRY
from .prompts import (FIXER_SYSTEM, GATE_SYSTEM, INVESTIGATOR_SYSTEM, PLANNER_SYSTEM, TRIAGE_SYSTEM, criteria_block,
                      ticket_block)
from .schemas import CriteriaOut, FixOut, GateOut, InvestigationOut, PlanOut, TriageOut, parse_model
from .store import store
from .tasks import TaskSpec
from .tools import TOOL_NAMES, TOOL_SCHEMAS, Tools, walk_files
from .util import stable_json, trunc

_PROSE_TOOL_RE = re.compile(r"<tool_call>|\"name\"\s*:\s*\"(" + "|".join(TOOL_NAMES) + r")\"")
_DIFF_FENCE_RE = re.compile(r"```(?:diff|patch)?\s*\n((?:diff --git|--- ).*?)```", re.S)


def valid_ids(run, ids: list[str]) -> list[str]:
    """Keep only test IDs that exist in the frozen suite."""
    known = set(run.test_ids)
    return [i for i in ids if i in known]


def system_user(system: str, user: str) -> list[dict]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def sibling_block(nc: NodeCtx, task: TaskSpec) -> str:
    """The parent's other subtasks, so a split child fixes only its own part (siblings run in parallel)."""
    node = store().get_node(nc.node_id) or {}
    parent = store().get_node(node.get("parent_id") or "") if node.get("parent_id") else None
    if not parent:
        return ""
    try:
        subtasks = json.loads(parent.get("plan_json") or "{}").get("subtasks") or []
    except json.JSONDecodeError:
        return ""
    others = [s for s in subtasks if isinstance(s, dict) and s.get("title") and s["title"] != task.title]
    if not others:
        return ""
    lines = "\n".join(f"- {s['title']}: {s.get('goal', '')}" for s in others)
    return f"\n# Sibling tasks (handled by other agents in parallel; do NOT fix these)\n{lines}\n"


# ---------------------------------------------------------------------------
# Depth 0: triage and acceptance criteria
# ---------------------------------------------------------------------------


def nano_triage(nc: NodeCtx) -> dict:
    out = json_call(nc, "gate", system_user(TRIAGE_SYSTEM, ticket_block(nc.run)), TriageOut, thinking=False)
    data = out.model_dump()
    store().event(nc.run.id, nc.node_id, "plan",
                  f"triage: {len(out.symptoms)} symptoms, {out.independent_symptoms} independent", data)
    return data


def planner_define_success(nc: NodeCtx) -> list[dict]:
    """Plain-English acceptance criteria, each mapped to frozen test IDs. Every test belongs to some criterion."""
    run = nc.run
    user = (ticket_block(run) + "\n# Triage\n" + stable_json(run.triage) + "\n\n" + criteria_block(run)
            + "\n# Mode\ndefine_success\nWrite plain English acceptance criteria for this ticket and map each "
              "to the frozen test IDs that check it. Every test ID must belong to at least one criterion.")
    out = json_call(nc, "planner", system_user(PLANNER_SYSTEM, user), CriteriaOut, thinking=True,
                    model_role="planner_root")
    criteria = []
    covered: set[str] = set()
    for i, c in enumerate(out.criteria, 1):
        ids = valid_ids(run, c.test_ids)
        covered.update(ids)
        criteria.append({"id": c.id or f"C{i}", "text": c.text, "test_ids": ids})
    for tid in run.test_ids:
        if tid not in covered:
            criteria.append({"id": f"C{len(criteria) + 1}", "text": f"Frozen test {tid} passes", "test_ids": [tid]})
    store().event(run.id, nc.node_id, "plan", f"defined {len(criteria)} acceptance criteria", {"criteria": criteria})
    return criteria


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def planner_decompose(nc: NodeCtx, task: TaskSpec, context: str) -> PlanOut:
    """Split or atomic. Forced atomic at max depth; prefers split at depth 0 when triage found several symptoms."""
    run, depth = nc.run, nc.depth
    must_atomic = depth >= run.budget.max_depth
    prefer_split = depth == 0 and int(run.triage.get("independent_symptoms") or 1) > 1
    user = (ticket_block(run) + "\n" + criteria_block(run) + "\n# Shared memory\n" + context
            + "\n\n# Mode\ndecompose\n" + sibling_block(nc, task)
            + f"# Task (depth {depth} of max {run.budget.max_depth})\nTitle: {task.title}\nGoal: {task.text()}\n"
            + f"Relevant tests: {', '.join(task.test_ids) or 'full suite'}\n"
            + ("This task MUST be atomic.\n" if must_atomic else "")
            + ("Triage found more than one independent symptom; prefer split.\n" if prefer_split else ""))
    out = json_call(nc, "planner", system_user(PLANNER_SYSTEM, user), PlanOut, thinking=True,
                    model_role="planner_root" if depth == 0 else None)
    out.kind = "split" if out.kind.strip().lower() == "split" and not must_atomic else "atomic"
    return out


# ---------------------------------------------------------------------------
# Leaf investigation: "Crusoe Reasoning + You.com Research"
# ---------------------------------------------------------------------------


def investigate(nc: NodeCtx, task: TaskSpec, context: str, extra: list[str], research_only: bool,
                plan: PlanOut) -> dict:
    """Lightning tool loop (max_tool_steps). Falls back to a one-shot snapshot if the model has no tool support."""
    run = nc.run
    user = (ticket_block(run) + "\n" + criteria_block(run) + "\n# Shared memory\n" + context + "\n"
            + ("\n# Findings from narrower research\n" + "\n".join(extra) + "\n" if extra else "")
            + sibling_block(nc, task)
            + f"\n# Your task (depth {nc.depth})\nTitle: {task.title}\nGoal: {task.text()}\n"
            + f"Planner approach: {plan.approach or 'n/a'}\n"
            + f"Relevant frozen tests: {', '.join(task.test_ids) or 'full suite'}\n"
            + ("Mode: research only. Collect and cite evidence; do not plan a code change.\n" if research_only
               else "Mode: find the root cause and the change needed.\n"))
    messages = system_user(INVESTIGATOR_SYSTEM, user)
    tools = Tools(nc)
    if "tools" in REGISTRY.supported(REGISTRY.model_for("investigator") or ""):
        result = _tool_loop(nc, messages, tools)
    else:
        snapshot = repo_snapshot(run, nc.wt) + "\n# Live /v1/models\n" + tools.t_list_crusoe_models()
        messages[1]["content"] += "\n# Repository snapshot\n" + snapshot
        result = json_call(nc, "investigator", messages, InvestigationOut, thinking=True, max_tokens=8192)
    evidence = result.model_dump()
    for item in result.evidence:   # web evidence must be a URL this run's searches returned
        if item.kind == "web" and item.ref in run.seen_urls:
            meta = run.seen_urls[item.ref]
            store().citation(run.id, nc.node_id, item.ref, meta.get("title", ""), meta.get("snippet", ""))
    store().update_node(nc.node_id, evidence_json=stable_json(evidence))
    return evidence


def _tool_loop(nc: NodeCtx, messages: list[dict], tools: Tools) -> InvestigationOut:
    max_tokens, raised, step = 8192, False, 0
    while step < nc.budget.max_tool_steps:
        nc.run.check_cancel()
        res = LLM.chat(nc, "investigator", messages, thinking=True, max_tokens=max_tokens, tools=TOOL_SCHEMAS)
        if res.tool_calls:
            # Append the assistant message (with its tool_calls) first, then one tool message per call.
            messages.append({"role": "assistant", "content": res.content or "", "tool_calls": res.tool_calls})
            for tc in res.tool_calls:
                out = tools.dispatch(tc["function"]["name"], tc["function"]["arguments"])
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
            step += 1
            continue
        parsed, _ = parse_model(InvestigationOut, res.content)
        if parsed:
            return parsed
        if _PROSE_TOOL_RE.search(res.content or "") and not raised:
            raised = True   # the model described a tool call in prose: raise max_tokens before anything else
            max_tokens *= 2
            continue
        messages.append({"role": "assistant", "content": res.content or ""})
        messages.append({"role": "user", "content": "Return your final answer now as one JSON object only."})
        break
    if messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": "Stop calling tools. Return your final answer now as one JSON "
                                                    "object only."})
    return json_call(nc, "investigator", messages, InvestigationOut, thinking=True, max_tokens=8192)


def nano_evidence_gate(nc: NodeCtx, task: TaskSpec, evidence: dict) -> GateOut:
    """Flowchart: "Enough evidence?" """
    user = f"# Task\nTitle: {task.title}\nGoal: {task.text()}\n\n# Collected evidence\n{stable_json(evidence)}\n"
    return json_call(nc, "gate", system_user(GATE_SYSTEM, user), GateOut, thinking=False)


# ---------------------------------------------------------------------------
# Fix proposal: "Propose and apply safe fix"
# ---------------------------------------------------------------------------


def repo_snapshot(run, wt: Path, limit: int = 60000, priority: Optional[list[str]] = None) -> str:
    """Editable source files followed by the frozen tests (read-only), as plain text for the prompt.
    Files in `priority` (e.g. named in the evidence) come first so they survive truncation on larger folders."""
    frozen = set(run.frozen_files)
    first = [p for p in (priority or []) if (wt / p).is_file()]
    ordered = first + [r for r in walk_files(wt, wt) if r not in first]
    src, tests, used = [], [], 0
    for rel in ordered:
        try:
            text = (wt / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        block = f"--- {rel} ---\n{text}"
        if rel in frozen:
            tests.append(block)
        elif used + len(block) <= limit:
            src.append(block)
            used += len(block)
    omitted = sum(1 for r in ordered if r not in frozen) - len(src)
    out = "## Source files (editable)\n" + "\n".join(src)
    if omitted > 0:
        out += f"\n[{omitted} more source files omitted for length; they exist in the worktree]"
    return out + "\n## Frozen acceptance tests (read-only, never edit)\n" + trunc("\n".join(tests), limit // 2)


def evidence_paths(evidence: dict) -> list[str]:
    """Repo paths mentioned in the investigator's evidence refs, e.g. "app/config.py" or "app/config.py:12"."""
    out = []
    for item in evidence.get("evidence") or []:
        ref = str(item.get("ref", "")).split("::")[0].split(":")[0].strip()
        if ref and not ref.startswith("http") and ("/" in ref or "." in ref) and ref not in out:
            out.append(ref)
    return out


def propose_fix(nc: NodeCtx, task: TaskSpec, evidence: dict, extra: list[str]) -> FixOut:
    run = nc.run
    context = store().context_for(run.id, nc.node_id)
    user = (ticket_block(run) + "\n" + criteria_block(run) + "\n# Shared memory\n" + context + "\n"
            + ("\n# Findings from narrower research\n" + "\n".join(extra) + "\n" if extra else "")
            + "\n# Evidence\n" + stable_json(evidence) + "\n"
            + sibling_block(nc, task)
            + f"\n# Your task (depth {nc.depth})\nTitle: {task.title}\nGoal: {task.text()}\n"
            + f"Tests that must pass: {', '.join(task.test_ids) or 'full suite'}\n"
            + "\n# Current worktree\n" + repo_snapshot(run, nc.wt, priority=evidence_paths(evidence)))

    def diff_fallback(text: str) -> Optional[FixOut]:
        m = _DIFF_FENCE_RE.search(text or "")
        return FixOut(summary="diff extracted from fenced block", diff=m.group(1)) if m else None

    return json_call(nc, "fixer", system_user(FIXER_SYSTEM, user), FixOut, thinking=True, max_tokens=8192,
                     fallback=diff_fallback)
