"""SQLite store: one WAL-mode connection guarded by a lock. Every string written is redacted first."""
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from .util import now_iso, redact, stable_json, trunc

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, scenario TEXT, ticket_json TEXT, status TEXT,
    created_at TEXT, finished_at TEXT, frozen_hash TEXT, budget_json TEXT, final_patch TEXT,
    report_md TEXT, criteria_json TEXT, reason TEXT, source_dir TEXT, read_only INT, applied_at TEXT);
CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, run_id TEXT, parent_id TEXT, depth INT,
    title TEXT, task TEXT, kind TEXT, status TEXT, reason TEXT, model_roles_json TEXT,
    worktree TEXT, patch TEXT, proof_json TEXT, replans INT, created_at TEXT, closed_at TEXT,
    plan_json TEXT, evidence_json TEXT, base_commit TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, node_id TEXT,
    ts TEXT, type TEXT, message TEXT, data_json TEXT);
CREATE TABLE IF NOT EXISTS findings(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, node_id TEXT,
    polarity TEXT, summary TEXT, detail TEXT, patch TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS citations(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, node_id TEXT,
    url TEXT, title TEXT, excerpt TEXT, retrieved_at TEXT);
CREATE TABLE IF NOT EXISTS llm_calls(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, node_id TEXT,
    role TEXT, model TEXT, prompt_tokens INT, completion_tokens INT, cached_tokens INT,
    latency_ms INT, finish_reason TEXT, error TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS web_cache(key TEXT PRIMARY KEY, response_json TEXT, ts TEXT);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_nodes_run ON nodes(run_id);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id);
CREATE INDEX IF NOT EXISTS idx_llm_run ON llm_calls(run_id);
"""

NODE_COLUMNS = {
    "status", "reason", "kind", "model_roles_json", "patch", "proof_json", "replans", "closed_at",
    "plan_json", "evidence_json", "task", "title", "worktree", "base_commit",
}
RUN_COLUMNS = {
    "status", "finished_at", "frozen_hash", "final_patch", "report_md", "criteria_json", "reason", "budget_json",
    "applied_at",
}
# Columns added after the first release; created on older databases at startup.
ADDED_COLUMNS = {"runs": {"source_dir": "TEXT", "read_only": "INT", "applied_at": "TEXT"}}


class Store:
    def __init__(self, path: Path):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA_SQL)
            for table, cols in ADDED_COLUMNS.items():
                have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for col, ctype in cols.items():
                    if col not in have:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}")
            self.conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> int:
        params = tuple(redact(p) if isinstance(p, str) else p for p in params)
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.lastrowid

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- runs --
    def create_run(self, run_id: str, scenario: str, ticket: dict, budget: dict, source_dir: str,
                   read_only: bool) -> None:
        self.execute(
            "INSERT INTO runs(id, scenario, ticket_json, status, created_at, budget_json, source_dir, read_only)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (run_id, scenario, stable_json(ticket), "running", now_iso(), stable_json(budget), source_dir,
             int(read_only)),
        )

    def update_run(self, run_id: str, **fields: Any) -> None:
        cols = [k for k in fields if k in RUN_COLUMNS]
        if cols:
            self.execute(
                f"UPDATE runs SET {', '.join(c + '=?' for c in cols)} WHERE id=?",
                tuple(fields[c] for c in cols) + (run_id,),
            )

    def get_run(self, run_id: str) -> Optional[dict]:
        return self.one("SELECT * FROM runs WHERE id=?", (run_id,))

    # -- nodes --
    def create_node(self, node_id: str, run_id: str, parent_id: Optional[str], depth: int, title: str,
                    task: str, worktree: str, base_commit: str) -> None:
        self.execute(
            "INSERT INTO nodes(id, run_id, parent_id, depth, title, task, kind, status, reason, model_roles_json,"
            " worktree, patch, proof_json, replans, created_at, closed_at, plan_json, evidence_json, base_commit)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (node_id, run_id, parent_id, depth, title, task, "pending", "pending", "", "{}", worktree, "", "",
             0, now_iso(), None, "", "", base_commit),
        )

    def update_node(self, node_id: str, **fields: Any) -> None:
        cols = [k for k in fields if k in NODE_COLUMNS]
        if cols:
            self.execute(
                f"UPDATE nodes SET {', '.join(c + '=?' for c in cols)} WHERE id=?",
                tuple(fields[c] for c in cols) + (node_id,),
            )

    def get_node(self, node_id: str) -> Optional[dict]:
        return self.one("SELECT * FROM nodes WHERE id=?", (node_id,))

    # -- events / findings / citations / llm calls --
    def event(self, run_id: str, node_id: Optional[str], etype: str, message: str, data: Any = None) -> int:
        data_json = "" if data is None else trunc(stable_json(data), 20000)
        return self.execute(
            "INSERT INTO events(run_id, node_id, ts, type, message, data_json) VALUES (?,?,?,?,?,?)",
            (run_id, node_id, now_iso(), etype, trunc(message, 4000), data_json),
        )

    def finding(self, run_id: str, node_id: str, polarity: str, summary: str, detail: str = "",
                patch: str = "") -> None:
        self.execute(
            "INSERT INTO findings(run_id, node_id, polarity, summary, detail, patch, ts) VALUES (?,?,?,?,?,?,?)",
            (run_id, node_id, polarity, trunc(summary, 1000), trunc(detail, 8000), trunc(patch, 20000), now_iso()),
        )
        self.event(run_id, node_id, "finding", f"[{polarity}] {trunc(summary, 300)}",
                   {"polarity": polarity, "detail": trunc(detail, 1500)})

    def citation(self, run_id: str, node_id: str, url: str, title: str, excerpt: str) -> None:
        exists = self.one("SELECT id FROM citations WHERE run_id=? AND node_id=? AND url=?", (run_id, node_id, url))
        if not exists:
            self.execute(
                "INSERT INTO citations(run_id, node_id, url, title, excerpt, retrieved_at) VALUES (?,?,?,?,?,?)",
                (run_id, node_id, url, trunc(title, 300), (excerpt or "")[:500], now_iso()),
            )

    def llm_call(self, run_id: str, node_id: str, role: str, model: str, prompt_tokens: int,
                 completion_tokens: int, cached_tokens: int, latency_ms: int, finish_reason: str,
                 error: Optional[str]) -> None:
        self.execute(
            "INSERT INTO llm_calls(run_id, node_id, role, model, prompt_tokens, completion_tokens, cached_tokens,"
            " latency_ms, finish_reason, error, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, node_id, role, model, prompt_tokens, completion_tokens, cached_tokens, latency_ms,
             finish_reason, error, now_iso()),
        )

    def cache_get(self, key: str) -> Optional[Any]:
        row = self.one("SELECT response_json FROM web_cache WHERE key=?", (key,))
        return json.loads(row["response_json"]) if row else None

    def cache_set(self, key: str, value: Any) -> None:
        self.execute("INSERT OR REPLACE INTO web_cache(key, response_json, ts) VALUES (?,?,?)",
                     (key, json.dumps(value), now_iso()))

    # -- F5: memory in --
    def context_for(self, run_id: str, node_id: str) -> str:
        """Ancestors' tasks and findings, this node's earlier negative findings, siblings' negative findings,
        and run-level citations (deduplicated by URL)."""
        node = self.get_node(node_id)
        if not node:
            return "(no prior context)"
        parts: list[str] = []

        def fmt_findings(rows: list[dict], indent: str) -> None:
            for f in rows[-8:]:
                line = f"{indent}- ({f['polarity']}) {trunc(f['summary'], 400)}"
                if f.get("detail"):
                    line += f"\n{indent}  detail: {trunc(f['detail'], 700)}"
                if f.get("patch"):
                    line += f"\n{indent}  patch:\n{trunc(f['patch'], 1500)}"
                parts.append(line)

        chain = []
        pid = node["parent_id"]
        while pid:
            anc = self.get_node(pid)
            if not anc:
                break
            chain.append(anc)
            pid = anc["parent_id"]
        chain.reverse()
        if chain:
            parts.append("## Ancestor tasks and their findings (root first)")
            for anc in chain:
                parts.append(f"- [depth {anc['depth']}] {anc['title']}: {trunc(anc['task'], 500)}")
                fmt_findings(self.query("SELECT * FROM findings WHERE node_id=? ORDER BY id", (anc["id"],)), "  ")
        own = self.query("SELECT * FROM findings WHERE node_id=? AND polarity='negative' ORDER BY id", (node_id,))
        if own:
            parts.append("## Negative findings from earlier attempts on this same task")
            fmt_findings(own, "")
        if node["parent_id"]:
            sibs = self.query(
                "SELECT f.*, n.title AS node_title FROM findings f JOIN nodes n ON f.node_id = n.id "
                "WHERE n.parent_id=? AND n.id<>? AND f.polarity='negative' ORDER BY f.id",
                (node["parent_id"], node_id),
            )
            if sibs:
                parts.append("## Negative findings from sibling tasks")
                for f in sibs[-10:]:
                    parts.append(f"- [{f['node_title']}] {trunc(f['summary'], 400)}")
        cites = self.query("SELECT url, title, excerpt FROM citations WHERE run_id=? ORDER BY id", (run_id,))
        seen: set[str] = set()
        cite_lines = []
        for c in cites:
            if c["url"] in seen:
                continue
            seen.add(c["url"])
            cite_lines.append(f"- {c['title']} ({c['url']}): {trunc(c['excerpt'], 200)}")
        if cite_lines:
            parts.append("## Citations already collected in this run")
            parts.extend(cite_lines[:12])
        return "\n".join(parts) if parts else "(no prior context)"


_STORE: Optional[Store] = None


def init_store(path: Path) -> Store:
    global _STORE
    _STORE = Store(path)
    return _STORE


def store() -> Store:
    assert _STORE is not None, "store not initialised; call init_store() first"
    return _STORE
