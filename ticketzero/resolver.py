"""The recursive resolver: one function, `Resolver.resolve`, called at every depth.

"Depth 0 / 1 / 2" in the flowchart are values of the depth argument, not different agents. Every node in the
execution tree is one resolve call and one row in the nodes table.

  resolve ── F1 entry gate ── plan ──┬── split: spawn children ─ join (F2) ─ merge (F3) ─ re-verify ─ replan / escalate
                                     └── leaf:  investigate ─ evidence gate ─ fix ─ verify ─ failure child / escalate
"""
import concurrent.futures
import json
import traceback
from pathlib import Path
from typing import Any, Optional

from . import agents
from .budget import NodeBudget, NodeCtx, RunState
from .errors import BudgetExhausted, LLMError, NodeFailed, RunCancelled
from .patches import validate_patch
from .schemas import PlanOut
from .store import store
from .tasks import NodeResult, TaskSpec
from .util import new_id, now_iso, redact, stable_json, trunc
from .verify import collect_tests, independent_verify


class Resolver:
    def __init__(self, run: RunState):
        self.run = run

    # ------------------------------------------------------------------
    # Node bookkeeping
    # ------------------------------------------------------------------

    def open_node(self, node_id: str, task: TaskSpec, depth: int, parent_id: Optional[str], wt: Path,
                  base: str) -> None:
        store().create_node(node_id, self.run.id, parent_id, depth, task.title, task.text(), str(wt), base)
        store().event(self.run.id, node_id, "node_opened", f"[depth {depth}] {task.title}",
                      {"parent_id": parent_id, "depth": depth, "test_ids": task.test_ids})

    def set_status(self, node_id: str, status: str, message: str = "") -> None:
        store().update_node(node_id, status=status)
        store().event(self.run.id, node_id, "status_changed", f"-> {status}" + (f": {message}" if message else ""),
                      {"status": status})

    def negative(self, node_id: str, summary: str, detail: str = "", patch: str = "") -> None:
        store().finding(self.run.id, node_id, "negative", summary, detail, patch)   # F5

    def close(self, node_id: str, status: str, reason: str = "", patch: str = "", proof: Optional[dict] = None,
              evidence: Optional[dict] = None, title: str = "") -> NodeResult:
        """Every close writes the node's findings to shared memory (F5), positive or negative."""
        fields: dict[str, Any] = {"status": status, "reason": reason, "closed_at": now_iso()}
        if patch:
            fields["patch"] = patch
        if proof is not None:
            fields["proof_json"] = stable_json(proof)
        if evidence is not None:
            fields["evidence_json"] = stable_json(evidence)
        store().update_node(node_id, **fields)
        store().event(self.run.id, node_id, "status_changed", f"-> {status}: {trunc(reason, 300)}",
                      {"status": status, "reason": reason})
        findings_text = ""
        if evidence:
            findings_text = f"Root cause: {evidence.get('root_cause', '')}\n" + "\n".join(
                f"- [{e.get('kind')}] {e.get('ref')}: {e.get('detail')}" for e in evidence.get("evidence", [])[:10])
        if status == "verified":
            store().finding(self.run.id, node_id, "positive", f"Verified: {reason or title}",
                            findings_text or (proof or {}).get("summary", ""), patch)
        else:
            store().finding(self.run.id, node_id, "negative", f"{status}: {reason}", findings_text)
            if status == "escalated":
                store().event(self.run.id, node_id, "escalated", reason)
        return NodeResult(node_id, status, reason, patch, proof, findings_text, title)

    def escalate_reason(self, reason: str) -> str:
        """Prefix the reason when the run budget is the real cause."""
        return f"budget or depth exceeded ({self.run.exhausted_reason()}); {reason}" if self.run.exhausted() else reason

    def spawn_child(self, nc: NodeCtx, task: TaskSpec, *, research_only: bool = False,
                    fraction: float = 1.0) -> NodeResult:
        """Run one child node in its own worktree (F3) with a slice of this node's calls, then refund the rest."""
        run = self.run
        cid = new_id("n")
        cwt, cbase = run.sandbox.create_worktree(nc.wt, cid)
        cnb = nc.budget.slice(run.carve(nc.budget, 1, fraction))
        store().event(run.id, nc.node_id, "child_spawned", f"{task.title} (calls slice {cnb.llm_calls})",
                      {"child_id": cid, "research_only": research_only, "test_ids": task.test_ids})
        result = self.resolve(task, nc.depth + 1, nc.node_id, cwt, cbase, cnb, research_only=research_only,
                              node_id=cid)
        run.refund(nc.budget, [cnb])
        return result

    # ------------------------------------------------------------------
    # The one recursive function
    # ------------------------------------------------------------------

    def resolve(self, task: TaskSpec, depth: int, parent_id: Optional[str], wt: Path, base: str, nb: NodeBudget,
                research_only: bool = False, node_id: Optional[str] = None, existing: bool = False) -> NodeResult:
        run = self.run
        node_id = node_id or new_id("n")
        if existing:   # replan: same node lineage
            store().update_node(node_id, task=task.text(), status="pending")
        else:
            self.open_node(node_id, task, depth, parent_id, wt, base)
        # F1: entry gate, runs on EVERY call including failure and replan recursion
        if run.cancelled:
            return self.close(node_id, "cancelled", "run cancelled", title=task.title)
        if depth > run.budget.max_depth or run.exhausted() or nb.llm_calls <= 0:
            why = (f"depth {depth} > max_depth {run.budget.max_depth}" if depth > run.budget.max_depth
                   else run.exhausted_reason() or f"node call slice {nb.llm_calls}")
            store().event(run.id, node_id, "budget", f"F1 gate stopped node: {why}")
            return self.close(node_id, "escalated", f"budget or depth exceeded ({why})", title=task.title)
        nc = NodeCtx(run, node_id, depth, nb, wt, base)
        try:
            return self._resolve_body(nc, task, parent_id, research_only)
        except BudgetExhausted as e:
            return self.close(node_id, "escalated", f"budget or depth exceeded (budget exhausted: {e})",
                              title=task.title)
        except RunCancelled:
            return self.close(node_id, "cancelled", "run cancelled", title=task.title)
        except NodeFailed as e:
            return self.close(node_id, "failed", str(e), title=task.title)
        except LLMError as e:   # e.g. Crusoe rate limit persisted through all retries
            return self.close(node_id, "failed", f"Crusoe API error: {e}", title=task.title)
        except Exception as e:  # never let one node crash the tree
            store().event(run.id, node_id, "status_changed", "unexpected error",
                          {"traceback": redact(traceback.format_exc()[-3000:])})
            return self.close(node_id, "failed", f"unexpected error: {type(e).__name__}: {redact(str(e))[:300]}",
                              title=task.title)

    def _resolve_body(self, nc: NodeCtx, task: TaskSpec, parent_id: Optional[str], research_only: bool) -> NodeResult:
        self.set_status(nc.node_id, "planning")
        if nc.depth == 0 and self.run.frozen_hash is None:
            self._define_success(nc)
        context = store().context_for(self.run.id, nc.node_id)   # F5: memory in
        task, plan = self._choose_plan(nc, task, context, research_only)
        if plan.kind == "split":
            return self.split(nc, task, plan, parent_id)
        return self.leaf(nc, task, context, research_only, plan)

    def _define_success(self, nc: NodeCtx) -> None:
        """Flowchart: "Crusoe Planner: define success". Freeze tests first (F6), then triage and criteria."""
        run = self.run
        run.sandbox.freeze(nc.wt)
        run.test_ids = collect_tests(run, nc.wt)
        run.live_test_ids = collect_tests(run, nc.wt, "live")
        store().update_run(run.id, frozen_hash=run.frozen_hash)
        store().event(run.id, nc.node_id, "plan", f"froze {len(run.frozen_files)} test/config files",
                      {"frozen_files": run.frozen_files, "frozen_hash": run.frozen_hash, "test_ids": run.test_ids,
                       "live_test_ids": run.live_test_ids})
        run.triage = agents.nano_triage(nc)
        run.criteria = agents.planner_define_success(nc)
        store().update_run(run.id, criteria_json=stable_json(run.criteria))

    def _choose_plan(self, nc: NodeCtx, task: TaskSpec, context: str, research_only: bool) -> tuple[TaskSpec, PlanOut]:
        """Flowchart: "decompose". Research nodes and max-depth nodes are atomic without an LLM call."""
        run = self.run
        if research_only:
            plan = PlanOut(kind="atomic", rationale="research-only node", approach="collect evidence")
        elif nc.depth >= run.budget.max_depth:
            plan = PlanOut(kind="atomic", rationale=f"forced atomic at max_depth {run.budget.max_depth}",
                           approach=task.goal, test_ids=task.test_ids)
        else:
            plan = agents.planner_decompose(nc, task, context)
        if plan.kind == "split" and len(plan.subtasks) < 2:   # a one-child split is just an atomic task
            if plan.subtasks:
                task = TaskSpec(task.title, task.goal, plan.subtasks[0].hypothesis or task.hypothesis,
                                task.test_ids or plan.subtasks[0].test_ids, task.note)
            plan.kind = "atomic"
        if plan.kind != "split" and plan.test_ids and not task.test_ids and nc.depth > 0:
            task.test_ids = agents.valid_ids(run, plan.test_ids)
        store().update_node(nc.node_id, kind="research" if research_only else plan.kind,
                            plan_json=stable_json(plan.model_dump()))
        store().event(run.id, nc.node_id, "plan", f"{plan.kind}: {trunc(plan.rationale or plan.approach, 300)}",
                      plan.model_dump())
        return task, plan

    # ------------------------------------------------------------------
    # Split branch
    # ------------------------------------------------------------------

    def split(self, nc: NodeCtx, task: TaskSpec, plan: PlanOut, parent_id: Optional[str]) -> NodeResult:
        results = self._run_children(nc, plan)                 # F2: returns only after ALL children finish
        self.set_status(nc.node_id, "fixing", "merging verified child patches")
        self._merge_children(nc, results)                      # F3: only the parent writes its worktree
        return self._reverify_or_replan(nc, task, results, parent_id)

    def _run_children(self, nc: NodeCtx, plan: PlanOut) -> list[NodeResult]:
        """Flowchart: "Spawn child investigation tasks", in parallel when the plan allows."""
        run = self.run
        subtasks = plan.subtasks[:4]
        self.set_status(nc.node_id, "waiting_children", f"{len(subtasks)} children")
        per = run.carve(nc.budget, len(subtasks))
        children = []
        for sub in subtasks:
            cid = new_id("n")
            cwt, cbase = run.sandbox.create_worktree(nc.wt, cid)   # F3
            ctask = TaskSpec(sub.title, sub.goal or sub.title, sub.hypothesis, agents.valid_ids(run, sub.test_ids))
            children.append((cid, ctask, cwt, cbase, nc.budget.slice(per)))
            store().event(run.id, nc.node_id, "child_spawned", f"{sub.title} (calls slice {per})",
                          {"child_id": cid, "worktree": str(cwt), "test_ids": ctask.test_ids})

        def run_child(c: tuple) -> NodeResult:
            cid, ctask, cwt, cbase, cnb = c
            return self.resolve(ctask, nc.depth + 1, nc.node_id, cwt, cbase, cnb, node_id=cid)

        if plan.parallel and len(children) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, run.budget.max_parallel_children)) as ex:
                results = list(ex.map(run_child, children))
        else:
            results = [run_child(c) for c in children]
        # F2: join barrier, all children have returned here
        run.refund(nc.budget, [c[4] for c in children])
        store().event(run.id, nc.node_id, "children_joined", ", ".join(f"{r.title}: {r.status}" for r in results),
                      {"results": [{"node_id": r.node_id, "status": r.status, "reason": r.reason} for r in results]})
        run.check_cancel()
        return results

    def _merge_children(self, nc: NodeCtx, results: list[NodeResult]) -> None:
        """Apply each verified child patch; a merge conflict becomes a child subproblem."""
        run = self.run
        for r in (r for r in results if r.status == "verified" and r.patch):
            ok, why = validate_patch(run, r.patch, nc.wt, nc.base)
            applied, err = run.sandbox.apply(nc.wt, r.patch) if ok else (False, why)
            store().event(run.id, nc.node_id, "merge", f"{'merged' if applied else 'conflict'}: {r.title}",
                          {"child_id": r.node_id, "ok": applied, "error": err})
            if applied:
                continue
            self.negative(nc.node_id, f"merge conflict applying patch from '{r.title}'", err, r.patch)
            child = self.spawn_child(nc, TaskSpec(
                f"Resolve merge conflict: {r.title}",
                f"Re-implement the intent of the child patch for '{r.title}' on top of the current code.",
                note=f"Conflicting patch:\n{trunc(r.patch, 3000)}\nApply error: {trunc(err, 500)}",
                test_ids=self._child_tests(r.node_id)))
            if child.status == "verified" and child.patch:
                ok2, err2 = run.sandbox.apply(nc.wt, child.patch)
                store().event(run.id, nc.node_id, "merge", f"{'merged' if ok2 else 'conflict'}: {child.title}",
                              {"child_id": child.node_id, "ok": ok2, "error": err2})

    def _reverify_or_replan(self, nc: NodeCtx, task: TaskSpec, results: list[NodeResult],
                            parent_id: Optional[str]) -> NodeResult:
        """Flowchart: "Reverify original parent goal" -> "All acceptance criteria pass?" -> VERIFIED or Replan."""
        run = self.run
        self.set_status(nc.node_id, "verifying", "full suite")
        diff = run.sandbox.diff(nc.wt, nc.base)
        verdict = independent_verify(nc, task.test_ids if nc.depth > 0 else None, diff)
        if verdict["passed"]:
            # Flowchart: "VERIFIED + Save Shared Memory"
            return self.close(nc.node_id, "verified", "children merged and parent goal re-verified", diff, verdict,
                              title=task.title)
        self.negative(nc.node_id, f"parent re-verification failed: {verdict['summary']}",
                      trunc((verdict.get("tests") or {}).get("output_tail", ""), 2000), diff)   # F5
        # F4: children that escalated or failed bubble up here; the parent decides.
        replans = int((store().get_node(nc.node_id) or {}).get("replans") or 0)
        if replans < nc.budget.max_replans:
            store().update_node(nc.node_id, replans=replans + 1)
            note = self._replan_note(verdict, results)
            store().event(run.id, nc.node_id, "plan", f"replan {replans + 1}/{nc.budget.max_replans}", {"note": note})
            # Flowchart: "Replan within depth / budget limit": re-enter at the SAME depth with the same node and
            # worktree; the F1 gate applies again.
            new_task = TaskSpec(task.title, task.goal, task.hypothesis, task.test_ids,
                                (task.note + "\n" if task.note else "") + note)
            return self.resolve(new_task, nc.depth, parent_id, nc.wt, nc.base, nc.budget, node_id=nc.node_id,
                                existing=True)
        return self.close(nc.node_id, "escalated", self.escalate_reason("parent goal not met after replans"),
                          diff, verdict, title=task.title)

    def _replan_note(self, verdict: dict, results: list[NodeResult]) -> str:
        lines = [f"Replan after failed verification: {verdict.get('summary', '')}"]
        failing = [k for k, v in ((verdict.get("tests") or {}).get("results") or {}).items() if v != "passed"]
        if failing:
            lines.append("Still failing tests: " + ", ".join(failing))
        for r in results:
            lines.append(f"Child '{r.title}' ended {r.status}" + (f": {trunc(r.reason, 200)}" if r.reason else "")
                         + (" (patch merged)" if r.status == "verified" and r.patch else ""))
        return "\n".join(lines)

    def _child_tests(self, child_id: str) -> list[str]:
        row = store().get_node(child_id) or {}
        try:
            plan = json.loads(row.get("plan_json") or "{}")
        except json.JSONDecodeError:
            plan = {}
        return agents.valid_ids(self.run, plan.get("test_ids") or [])

    # ------------------------------------------------------------------
    # Atomic leaf
    # ------------------------------------------------------------------

    def leaf(self, nc: NodeCtx, task: TaskSpec, context: str, research_only: bool, plan: PlanOut) -> NodeResult:
        enough, evidence, extra = self._gather_evidence(nc, task, context, research_only, plan)
        if not enough:
            return self.close(nc.node_id, "escalated", "insufficient evidence", evidence=evidence,
                              title=task.title)   # F4: bubbles up
        store().finding(self.run.id, nc.node_id, "positive",
                        f"Root cause: {trunc(evidence.get('root_cause', ''), 400)}",
                        stable_json(evidence.get("evidence", [])[:10]))
        if research_only:   # research nodes return findings, no patch
            return self.close(nc.node_id, "verified", "research findings collected", evidence=evidence,
                              title=task.title)
        return self._fix_loop(nc, task, evidence, extra)

    def _gather_evidence(self, nc: NodeCtx, task: TaskSpec, context: str, research_only: bool,
                         plan: PlanOut) -> tuple[bool, dict, list[str]]:
        """Flowchart: "Crusoe Reasoning + You.com Research" -> "Enough evidence?" -> narrower research child."""
        run = self.run
        self.set_status(nc.node_id, "investigating")
        extra: list[str] = []
        evidence: dict = {}
        for rnd in range(nc.budget.max_evidence_rounds):
            evidence = agents.investigate(nc, task, context, extra, research_only, plan)
            gate = agents.nano_evidence_gate(nc, task, evidence)
            store().event(run.id, nc.node_id, "evidence_gate",
                          f"round {rnd + 1}: enough={gate.enough}" + (f", missing: {gate.missing}" if gate.missing else ""),
                          gate.model_dump())
            if gate.enough:
                return True, evidence, extra
            self.negative(nc.node_id, f"evidence round {rnd + 1} insufficient: {gate.missing}",
                          evidence.get("root_cause", ""))
            if rnd == nc.budget.max_evidence_rounds - 1:
                break   # no point researching further: nothing would consume the findings
            # Flowchart: "No -> Spawn narrower subproblem" -> "Run tools and collect proof"
            sub = self.spawn_child(nc, TaskSpec(f"Research only: {trunc(gate.missing, 90)}",
                                                gate.missing or task.goal),
                                   research_only=True, fraction=0.5)
            if sub.findings:
                extra.append(f"Research child '{sub.title}' ({sub.status}):\n{sub.findings}")   # "Return findings"
            if sub.status == "escalated" and run.exhausted():
                break
        return False, evidence, extra

    def _fix_loop(self, nc: NodeCtx, task: TaskSpec, evidence: dict, extra: list[str]) -> NodeResult:
        """Flowchart: "Propose and apply safe fix" -> "Independent Verifier" -> "Tests pass?"."""
        for attempt in range(1, nc.budget.max_fix_attempts + 1):
            self.run.check_cancel()
            self.set_status(nc.node_id, "fixing", f"attempt {attempt}/{nc.budget.max_fix_attempts}")
            applied = self._propose_and_apply(nc, task, evidence, extra)
            if applied is None:
                continue
            fix_summary, actual = applied
            self.set_status(nc.node_id, "verifying", f"attempt {attempt}")
            verdict = independent_verify(nc, task.test_ids or None, actual)
            if verdict["passed"]:
                # Flowchart: "Close child task"
                return self.close(nc.node_id, "verified", fix_summary or "fix verified", actual, verdict, evidence,
                                  title=task.title)
            self.negative(nc.node_id, f"fix attempt {attempt} failed verification: {verdict['summary']}",
                          trunc((verdict.get("tests") or {}).get("output_tail", ""), 2500), actual)   # F5
            self.run.sandbox.reset_hard(nc.wt, nc.base)
            recovered = self._recover_via_child(nc, task, verdict, actual, attempt, evidence)
            if recovered:
                return recovered
        return self.close(nc.node_id, "escalated", self.escalate_reason("fix attempts exhausted"), evidence=evidence,
                          title=task.title)   # F4: bubbles up

    def _propose_and_apply(self, nc: NodeCtx, task: TaskSpec, evidence: dict,
                           extra: list[str]) -> Optional[tuple[str, str]]:
        """Fixer proposes a diff; validate (F6), apply, re-validate the real change. Returns (summary, diff)."""
        run, nid = self.run, nc.node_id
        fix = agents.propose_fix(nc, task, evidence, extra)
        store().event(run.id, nid, "patch_proposed", fix.summary or "(no summary)", {"diff": trunc(fix.diff, 6000)})
        ok, why = validate_patch(run, fix.diff, nc.wt, nc.base)
        if not ok:
            store().event(run.id, nid, "patch_rejected", why)
            self.negative(nid, f"patch rejected by validator: {why}", fix.summary, fix.diff)
            return None
        applied, err = run.sandbox.apply(nc.wt, fix.diff)
        if not applied:
            store().event(run.id, nid, "patch_rejected", f"patch did not apply: {trunc(err, 300)}")
            self.negative(nid, "patch did not apply to the current worktree", err, fix.diff)
            run.sandbox.reset_hard(nc.wt, nc.base)
            return None
        actual = run.sandbox.diff(nc.wt, nc.base)
        ok, why = validate_patch(run, actual, nc.wt, nc.base)
        if not ok:
            store().event(run.id, nid, "patch_rejected", f"applied change rejected: {why}")
            self.negative(nid, f"applied change rejected by validator: {why}", fix.summary, actual)
            run.sandbox.reset_hard(nc.wt, nc.base)
            return None
        return fix.summary, actual

    def _recover_via_child(self, nc: NodeCtx, task: TaskSpec, verdict: dict, failed_patch: str, attempt: int,
                           evidence: dict) -> Optional[NodeResult]:
        """Flowchart: "No -> Create new subproblem from failure" -> Depth+1 (bounded by the F1 gate)."""
        run = self.run
        failing = [k for k, v in ((verdict.get("tests") or {}).get("results") or {}).items() if v != "passed"]
        failing_names = ", ".join(f.split("::")[-1] for f in failing) or trunc(verdict["reason"], 80)
        child = self.spawn_child(nc, TaskSpec(
            f"Fix failed verification: {trunc(failing_names, 90)}",
            f"{task.goal}\nA previous patch for this task failed independent verification. "
            f"Produce a complete fix that makes the task's tests pass.",
            task.hypothesis, task.test_ids,
            note=f"Verifier summary: {verdict['summary']}\nFailing tests: {', '.join(failing) or 'n/a'}\n"
                 f"Failed patch (attempt {attempt}, do not repeat as-is):\n{trunc(failed_patch, 2500)}"))
        if child.status != "verified" or not child.patch:
            return None
        applied, err = run.sandbox.apply(nc.wt, child.patch)
        store().event(run.id, nc.node_id, "merge", f"{'merged' if applied else 'conflict'}: {child.title}",
                      {"child_id": child.node_id, "ok": applied, "error": err})
        if applied:
            actual = run.sandbox.diff(nc.wt, nc.base)
            self.set_status(nc.node_id, "verifying", "re-verify with child patch")
            verdict = independent_verify(nc, task.test_ids or None, actual)
            if verdict["passed"]:
                return self.close(nc.node_id, "verified", f"fixed via child: {child.reason}", actual, verdict,
                                  evidence, title=task.title)
            self.negative(nc.node_id, f"child patch failed re-verification: {verdict['summary']}", "", actual)
        run.sandbox.reset_hard(nc.wt, nc.base)
        return None
