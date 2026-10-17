"""Run lifecycle: start a run in a background thread, resolve from depth 0, write the outcome."""
import threading
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
import uuid

from . import config
from .budget import ACTIVE_LOCK, ACTIVE_RUNS, NODE_CAP_FIELDS, NodeBudget, RunBudget, RunState
from .reports import criteria_status, escalation_checks, usage_summary, write_report
from .resolver import Resolver
from .store import store
from .tasks import NodeResult, TaskSpec
from .util import now_iso, redact, stable_json, trunc
from .verify import run_pytest
from .workspace import FolderScan


def start_run(scenario: str, ticket: dict, scan: FolderScan, read_only: bool, budget: RunBudget, caps: dict) -> str:
    """Snapshot the working folder into a new run and resolve it in a background thread."""
    run_id = "r" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:6]
    run = RunState(run_id, scenario, ticket, scan.root, scan.files, budget, caps, python=scan.python,
                   read_only=read_only)
    store().create_run(run_id, scenario, ticket, {"run": asdict(budget), "node": caps, "live_tests": config.LIVE_TESTS},
                       str(scan.root), read_only)
    store().event(run_id, None, "budget", f"run created from folder {scan.root}",
                  {"run": asdict(budget), "node": caps, "folder": scan.summary()})
    with ACTIVE_LOCK:
        ACTIVE_RUNS[run_id] = run
    threading.Thread(target=execute_run, args=(run,), name=f"run-{run_id}", daemon=True).start()
    return run_id


def execute_run(run: RunState) -> None:
    try:
        base_commit = run.sandbox.init_base()
        t = run.ticket
        root_task = TaskSpec(trunc(t.get("title") or "Support ticket", 120),
                             f"Resolve the ticket: {t.get('description', '')}\nExpected behavior: "
                             f"{t.get('expected_behavior', '')}")
        caps = {k: v for k, v in run.node_caps.items() if k in NODE_CAP_FIELDS}
        nb = NodeBudget(llm_calls=run.budget.max_llm_calls, **caps)
        result = Resolver(run).resolve(root_task, 0, None, run.sandbox.base, base_commit, nb, node_id=run.root_id)
        finish_run(run, result)
    except Exception as e:
        store().update_run(run.id, status="failed", finished_at=now_iso(),
                           reason=f"{type(e).__name__}: {redact(str(e))[:500]}",
                           report_md=f"# TicketZero report: FAILED\n\nThe run crashed: `{redact(str(e))[:500]}`\n")
        store().event(run.id, None, "run_finished", f"run FAILED: {type(e).__name__}: {redact(str(e))[:300]}",
                      {"traceback": redact(traceback.format_exc()[-3000:])})
    finally:
        with ACTIVE_LOCK:
            ACTIVE_RUNS.pop(run.id, None)


def finish_run(run: RunState, result: NodeResult) -> None:
    """Only depth 0 turns "escalated" into "Escalate to human": it writes escalation.md (F4)."""
    if result.status == "verified" and result.proof and result.proof.get("scope") == "full_suite":
        final_results = (result.proof.get("tests") or {}).get("results") or {}
    elif run.frozen_hash:
        final_results = run_pytest(run, run.sandbox.base)["results"]
    else:
        final_results = {}
    crit = criteria_status(run.criteria, final_results)
    status = {"verified": "verified", "cancelled": "cancelled"}.get(result.status, "escalated")
    next_checks = escalation_checks(run, result) if status == "escalated" else []
    report = write_report(run, result, crit, next_checks)
    if status == "escalated":
        (run.sandbox.run_dir / "escalation.md").write_text(report, encoding="utf-8")
    store().update_run(run.id, status=status, finished_at=now_iso(), report_md=report,
                       final_patch=result.patch if status == "verified" else "", criteria_json=stable_json(crit),
                       reason=result.reason)
    store().event(run.id, run.root_id, "run_finished", f"run {status.upper()}: {result.reason}",
                  {"status": status, "usage": usage_summary(run.id)})
