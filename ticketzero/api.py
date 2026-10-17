"""FastAPI service.

Run:
  export CRUSOE_API_KEY='...'
  export YDC_API_KEY='...'
  uvicorn ticketzero.api:app --host 127.0.0.1 --port 8000
"""
import json
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from . import config
from .budget import ACTIVE_RUNS, build_budget
from .llm import LLM
from .models import REGISTRY
from .reports import build_tree, usage_summary
from .runner import start_run
from .scenarios import SCENARIOS, write_scenario_templates
from .store import init_store, store
from .patches import safe_rel
from .util import now_iso, trunc
from .workspace import FolderError, apply_to_folder, require_tests, scan_folder


def startup() -> None:
    """Create TICKETZERO_HOME, write scenario templates, open the DB, resolve models."""
    (config.HOME / "runs").mkdir(parents=True, exist_ok=True)
    write_scenario_templates()
    db = init_store(config.HOME / "ticketzero.db")
    for r in db.query("SELECT id FROM runs WHERE status='running'"):
        db.update_run(r["id"], status="failed", finished_at=now_iso(), reason="backend restarted during the run")
    REGISTRY.resolve()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    startup()
    yield


app = FastAPI(title="TicketZero", lifespan=lifespan)
# The UI is normally served through Vite's /api proxy. When its standalone index.html is opened from disk (origin
# "null") or served from another local port, it calls the backend directly; allow only those local origins.
app.add_middleware(CORSMiddleware, allow_origin_regex=r"^(null|https?://(127\.0\.0\.1|localhost)(:\d+)?)$",
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


class TicketIn(BaseModel):
    title: str = ""
    description: str = ""
    logs: str = ""
    expected_behavior: str = ""


class RunCreate(BaseModel):
    scenario: Optional[str] = None      # a demo: its own folder plus a preset ticket
    folder: Optional[str] = None        # or: the user's working folder ...
    ticket: Optional[TicketIn] = None   # ... and what is wrong with it
    budget: dict[str, Any] = {}


def _parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@app.get("/health")
def health() -> dict:
    if not REGISTRY.ok() and config.CRUSOE_API_KEY and time.monotonic() - REGISTRY.last_attempt > 30:
        REGISTRY.resolve()
    return {
        "ok": REGISTRY.ok(),
        "crusoe_key_present": bool(config.CRUSOE_API_KEY),
        "ydc_key_present": bool(config.YDC_API_KEY),
        "models": dict(REGISTRY.roles),
        "rpm_per_model": config.RPM_PER_MODEL,
        "live_tests": config.LIVE_TESTS,
        "error": REGISTRY.error,
        "notes": REGISTRY.notes,
        "rejected_chat_template_kwargs": {m: sorted(v) for m, v in LLM.rejected_ctk.items()},
        "model_info": REGISTRY.info,
        "resolved_at": REGISTRY.resolved_at,
    }


@app.get("/scenarios")
def scenarios() -> list[dict]:
    return [{"name": k, "title": v["title"], "description": v["description"], "expected_outcome": v["expected_outcome"],
             "label": v["label"], "hint": v["hint"], "folder": str(v["folder"]), "ticket": v["ticket"]}
            for k, v in SCENARIOS.items()]


@app.get("/fs/files")
def list_folder_files(path: str) -> dict:
    """The files a run on this folder would copy (secrets, dependencies and caches excluded), for browsing."""
    try:
        scan = scan_folder(path)
    except FolderError as e:
        raise HTTPException(400, str(e))
    return {**scan.summary(), "name": scan.root.name,
            "entries": [{"path": p, "size": len(b)} for p, b in sorted(scan.files.items())]}


PREVIEW_LIMIT = 256 * 1024


@app.get("/fs/file")
def read_folder_file(path: str, file: str) -> dict:
    """Text preview of one file that the scan includes. Excluded files (e.g. .env) are never served."""
    try:
        scan = scan_folder(path)
        rel = safe_rel(file)
    except (FolderError, ValueError) as e:
        raise HTTPException(400, str(e))
    data = scan.files.get(rel)
    if data is None:
        raise HTTPException(404, "This file is not available in the selected folder.")
    if len(data) > PREVIEW_LIMIT:
        raise HTTPException(413, "This file is larger than the 256 KB preview limit. Open it in your editor.")
    if b"\0" in data:
        raise HTTPException(415, "This file appears to be binary and cannot be previewed as text.")
    return {"path": rel, "text": data.decode("utf-8", errors="replace"), "size": len(data)}


@app.get("/fs/check")
def check_folder(path: str) -> dict:
    """What a run on this folder would see: file count, test files, test interpreter. 400 if unusable."""
    try:
        scan = scan_folder(path)
        require_tests(scan)
    except FolderError as e:
        raise HTTPException(400, str(e))
    return scan.summary()


@app.post("/fs/pick")
def pick_folder() -> dict:
    """Open the native folder picker on the machine running the backend (macOS). Returns {"path": ...} or
    {"path": null} when the user cancels."""
    if sys.platform != "darwin":
        raise HTTPException(501, "The native folder picker is only available on macOS; type the folder path instead.")
    script = 'POSIX path of (choose folder with prompt "Choose the working folder for TicketZero")'
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"path": None}
    if p.returncode != 0:   # cancelled
        return {"path": None}
    return {"path": p.stdout.strip().rstrip("/") or "/"}


@app.post("/runs")
def create_run(body: RunCreate) -> dict:
    if not REGISTRY.ok():
        raise HTTPException(503, f"Crusoe models are not resolved; new runs are refused. {REGISTRY.error or ''}".strip())
    if body.scenario:
        sc = SCENARIOS.get(body.scenario)
        if not sc:
            raise HTTPException(404, f"unknown scenario {body.scenario}")
        scenario, ticket, folder, read_only = body.scenario, sc["ticket"], str(sc["folder"]), True
    elif body.folder:
        ticket = (body.ticket or TicketIn()).model_dump()
        if not ticket["description"].strip():
            raise HTTPException(400, "Describe what is going wrong.")
        ticket["title"] = ticket["title"].strip() or trunc(ticket["description"].strip().splitlines()[0], 80)
        scenario, folder, read_only = "folder", body.folder, False
    else:
        raise HTTPException(400, "Provide a demo scenario, or a folder and a description of the problem.")
    try:
        scan = scan_folder(folder)
        require_tests(scan)
    except FolderError as e:
        raise HTTPException(400, str(e))
    budget, caps = build_budget(body.budget)
    return {"run_id": start_run(scenario, ticket, scan, read_only, budget, caps)}


@app.get("/runs")
def list_runs(limit: int = 30) -> list[dict]:
    return store().query("SELECT id, scenario, status, created_at, finished_at, reason, ticket_json FROM runs "
                         "ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),))


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    row = store().get_run(run_id)
    if not row:
        raise HTTPException(404, "run not found")
    active = ACTIVE_RUNS.get(run_id)
    ended = _parse_time(row["finished_at"]) if row["finished_at"] else datetime.now(timezone.utc)
    root = store().one("SELECT id FROM nodes WHERE run_id=? AND depth=0 ORDER BY created_at LIMIT 1", (run_id,))
    return {
        "id": row["id"], "scenario": row["scenario"], "status": row["status"], "reason": row["reason"],
        "ticket": json.loads(row["ticket_json"] or "{}"), "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "elapsed_seconds": int((ended - _parse_time(row["created_at"])).total_seconds()),
        "frozen_hash": row["frozen_hash"], "budget": json.loads(row["budget_json"] or "{}"),
        "usage": usage_summary(run_id),
        "calls_used_live": active.calls_used if active else None,
        "tokens_used_live": active.tokens_used if active else None,
        "criteria": json.loads(row["criteria_json"] or "[]"), "has_patch": bool(row["final_patch"]),
        "source_dir": row["source_dir"], "read_only": bool(row["read_only"]), "applied_at": row["applied_at"],
        "handoff": json.loads(row["handoff_json"]) if row["handoff_json"] else None,
        "root_node_id": active.root_id if active else (root or {}).get("id"),
    }


@app.get("/runs/{run_id}/tree")
def get_tree(run_id: str) -> list[dict]:
    if not store().get_run(run_id):
        raise HTTPException(404, "run not found")
    return build_tree(run_id)


@app.get("/runs/{run_id}/events")
def get_events(run_id: str, after_id: int = 0, limit: int = 500) -> list[dict]:
    return store().query("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
                         (run_id, after_id, max(1, min(limit, 2000))))


@app.get("/nodes/{node_id}")
def get_node(node_id: str) -> dict:
    node = store().get_node(node_id)
    if not node:
        raise HTTPException(404, "node not found")
    for k in ("plan_json", "proof_json", "evidence_json", "model_roles_json"):
        try:
            node[k.removesuffix("_json")] = json.loads(node.pop(k) or "null")
        except json.JSONDecodeError:
            node[k.removesuffix("_json")] = None
    node["findings"] = store().query("SELECT * FROM findings WHERE node_id=? ORDER BY id", (node_id,))
    node["citations"] = store().query("SELECT * FROM citations WHERE node_id=? ORDER BY id", (node_id,))
    node["llm_calls"] = store().query("SELECT * FROM llm_calls WHERE node_id=? ORDER BY id", (node_id,))
    return node


@app.get("/runs/{run_id}/report", response_class=PlainTextResponse)
def get_report(run_id: str) -> str:
    row = store().get_run(run_id)
    if not row:
        raise HTTPException(404, "run not found")
    if row["status"] == "running":
        return "# Run in progress\n\nThe report is written when the run finishes.\n"
    return row["report_md"] or "# No report\n"


@app.get("/runs/{run_id}/patch", response_class=PlainTextResponse)
def get_patch(run_id: str) -> str:
    row = store().get_run(run_id)
    if not row:
        raise HTTPException(404, "run not found")
    return row["final_patch"] or ""


@app.post("/runs/{run_id}/apply")
def apply_run(run_id: str) -> dict:
    """Write a verified run's changes into the user's folder. Only on request, never for demos, never twice, and
    never over files the user changed after the run started."""
    row = store().get_run(run_id)
    if not row:
        raise HTTPException(404, "run not found")
    if row["status"] != "verified" or not row["final_patch"]:
        raise HTTPException(409, "Only a verified run's changes can be applied.")
    if row["read_only"]:
        raise HTTPException(409, "Demo folders are read-only.")
    if row["applied_at"]:
        raise HTTPException(409, f"Already applied at {row['applied_at']}.")
    try:
        written = apply_to_folder(Path(row["source_dir"]), config.HOME / "runs" / run_id / "base", row["final_patch"])
    except FolderError as e:
        raise HTTPException(409, str(e))
    store().update_run(run_id, applied_at=now_iso())
    store().event(run_id, None, "merge", f"applied to {row['source_dir']}: {', '.join(written)}", {"files": written})
    return {"ok": True, "files": written}


@app.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str) -> dict:
    run = ACTIVE_RUNS.get(run_id)
    if not run:
        raise HTTPException(409, "run is not active")
    run.cancelled = True
    store().event(run_id, None, "budget", "cancel requested")
    return {"ok": True}
