"""Investigator tools. All file access is restricted to the node's worktree; there is no shell tool."""
import json
import os
import re
from pathlib import Path
from typing import Optional

import requests

from . import config, models
from .budget import NodeCtx
from .errors import GitError
from .research import YOU
from .store import store
from .util import now_iso, redact, stable_json, trunc
from .verify import run_pytest

# Fixed order and content: part of the cached prompt prefix.
TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "list_files", "description": "Recursively list files in the sandboxed repository (max 300).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "default": "."}}}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a text file with line numbers.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "start": {"type": "integer", "default": 1},
            "end": {"type": "integer", "default": 200}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "search_code", "description": "Search text files with a Python regex (max 50 matches).",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "parse_logs", "description": "Return the ticket's redacted error logs and an extracted summary.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "run_tests", "description": "Run the frozen acceptance tests (or a subset) in the sandbox.",
        "parameters": {"type": "object", "properties": {"test_ids": {"type": "array", "items": {"type": "string"}}}}}},
    {"type": "function", "function": {
        "name": "list_crusoe_models",
        "description": "Live GET /v1/models on Crusoe Managed Inference: id, context_length, supported_parameters, features.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "web_search", "description": "Search the web (You.com). Prefer official Crusoe docs and changelogs.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_page", "description": "Fetch a page as markdown. Only URLs returned by web_search in this run.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
]
TOOL_NAMES = [t["function"]["name"] for t in TOOL_SCHEMAS]


def resolve_in(wt: Path, path: str) -> Path:
    p = (path or ".").strip() or "."
    if os.path.isabs(p):
        raise ValueError("absolute paths are not allowed")
    target = (wt / p).resolve()
    root = wt.resolve()
    if target != root and root not in target.parents:
        raise ValueError("path resolves outside the worktree")
    if ".git" in target.relative_to(root).parts:
        raise ValueError("access to .git is not allowed")
    return target


def walk_files(wt: Path, start: Path) -> list[str]:
    out = []
    for root, dirs, files in os.walk(start):
        dirs[:] = sorted(d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache"))
        for name in sorted(files):
            out.append(Path(root, name).relative_to(wt).as_posix())
    return out


class Tools:
    def __init__(self, nc: NodeCtx):
        self.nc = nc

    def dispatch(self, name: str, args_json: str) -> str:
        """Run one tool call, log it as tool_call/tool_result events, and return redacted text for the model."""
        nc = self.nc
        try:
            args = json.loads(args_json or "{}")
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError:
            args = {}
        store().event(nc.run.id, nc.node_id, "tool_call", f"{name}({trunc(stable_json(args), 300)})", args)
        try:
            fn = getattr(self, f"t_{name}", None) if name in TOOL_NAMES else None
            out = fn(**args) if fn else f"error: unknown tool {name}"
        except TypeError as e:
            out = f"error: bad arguments for {name}: {e}"
        except (ValueError, OSError, re.error, GitError) as e:
            out = f"error: {e}"
        except requests.RequestException as e:
            out = f"error: network request failed: {type(e).__name__}"
        out = redact(out if isinstance(out, str) else stable_json(out))
        store().event(nc.run.id, nc.node_id, "tool_result", f"{name}: {trunc(out, 160)}", {"result": trunc(out, 2000)})
        return trunc(out, 9000)

    def t_list_files(self, path: str = ".") -> str:
        start = resolve_in(self.nc.wt, path)
        files = walk_files(self.nc.wt, start)
        return "\n".join(files[:300]) + (f"\n... ({len(files) - 300} more)" if len(files) > 300 else "")

    def t_read_file(self, path: str, start: int = 1, end: int = 200) -> str:
        f = resolve_in(self.nc.wt, path)
        if not f.is_file():
            return f"error: not a file: {path}"
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        start, end = max(1, int(start)), min(len(lines), int(end))
        body = "\n".join(f"{i:5d}| {lines[i - 1]}" for i in range(start, end + 1))
        return f"{path} (lines {start}-{end} of {len(lines)})\n{body}"

    def t_search_code(self, pattern: str) -> str:
        rx = re.compile(pattern)
        hits = []
        for rel in walk_files(self.nc.wt, self.nc.wt):
            f = self.nc.wt / rel
            if f.stat().st_size > 500_000:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= 50:
                        return "\n".join(hits)
        return "\n".join(hits) or "no matches"

    def t_parse_logs(self) -> str:
        return ("<logs>\n" + trunc(redact(self.nc.run.ticket.get("logs", "")), 6000) + "\n</logs>\n"
                "Extracted summary (Nemotron Nano triage): " + stable_json(self.nc.run.triage))

    def t_run_tests(self, test_ids: Optional[list[str]] = None) -> str:
        r = run_pytest(self.nc.run, self.nc.wt, test_ids if isinstance(test_ids, list) else None)
        return stable_json({"passed": r["passed"], "summary": r["summary"], "results": r["results"],
                            "output_tail": r["output_tail"]})

    def t_list_crusoe_models(self) -> str:
        catalog = models.fetch_crusoe_models()
        slim = [{"id": m.get("id"), "context_length": m.get("context_length"),
                 "supported_parameters": m.get("supported_parameters"), "features": m.get("features")}
                for m in catalog]
        return stable_json({"source": f"GET {config.CRUSOE_BASE_URL}/models (live)", "retrieved_at": now_iso(),
                            "models": slim})

    def t_web_search(self, query: str) -> str:
        r = YOU.search(self.nc.run, query)
        if "results" not in r:
            return stable_json(r)
        if not r["results"]:
            return f"No results for query: {r['query']}"
        parts = [f"Results for query: {r['query']}"]
        for item in r["results"]:
            parts.append(f'<untrusted_source url="{item["url"]}">\nTitle: {item["title"]}\n{item["snippet"]}\n'
                         f'</untrusted_source>')
        return "\n".join(parts)

    def t_fetch_page(self, url: str) -> str:
        r = YOU.contents(self.nc.run, url)
        if "markdown" not in r:
            return stable_json(r)
        store().citation(self.nc.run.id, self.nc.node_id, url, r["title"], r["markdown"][:500])
        return f'<untrusted_source url="{url}">\nTitle: {r["title"]}\n{r["markdown"]}\n</untrusted_source>'
