"""Run and node budgets, shared run state, and the per-node context passed to every call (F1)."""
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import config
from .errors import BudgetExhausted, RunCancelled
from .sandbox import Sandbox
from .util import new_id


@dataclass
class RunBudget:            # shared by the whole run, thread-safe via RunState.lock
    max_llm_calls: int = 120
    max_total_tokens: int = 1_500_000
    max_wall_seconds: int = 900
    max_depth: int = 3
    max_parallel_children: int = 3


@dataclass
class NodeBudget:           # per node slice
    llm_calls: int          # parent gives each child floor((parent_remaining - reserve) / n_children)
    max_replans: int = 2
    max_fix_attempts: int = 2
    max_evidence_rounds: int = 2
    max_tool_steps: int = 8  # tool-loop iterations per investigation round

    def slice(self, llm_calls: int) -> "NodeBudget":
        return NodeBudget(llm_calls, self.max_replans, self.max_fix_attempts, self.max_evidence_rounds,
                          self.max_tool_steps)


NODE_CAP_FIELDS = ("max_replans", "max_fix_attempts", "max_evidence_rounds", "max_tool_steps")

# Clamp ranges for per-run overrides sent by the frontend.
BUDGET_LIMITS = {
    "max_llm_calls": (1, 1000), "max_total_tokens": (1000, 20_000_000), "max_wall_seconds": (30, 7200),
    "max_depth": (0, 4), "max_parallel_children": (1, 8), "max_replans": (0, 5), "max_fix_attempts": (1, 5),
    "max_evidence_rounds": (1, 5), "max_tool_steps": (1, 30),
}


def build_budget(overrides: dict[str, Any]) -> tuple[RunBudget, dict[str, int]]:
    """Apply clamped overrides to the default run budget and node caps."""
    rb = RunBudget()
    caps = {k: getattr(NodeBudget(0), k) for k in NODE_CAP_FIELDS}
    for k, v in (overrides or {}).items():
        if k not in BUDGET_LIMITS:
            continue
        try:
            lo, hi = BUDGET_LIMITS[k]
            val = min(hi, max(lo, int(v)))
        except (TypeError, ValueError):
            continue
        if k in NODE_CAP_FIELDS:
            caps[k] = val
        else:
            setattr(rb, k, val)
    return rb, caps


class RunState:
    def __init__(self, run_id: str, scenario: str, ticket: dict, source_dir: Path, files: dict[str, bytes],
                 budget: RunBudget, node_caps: dict, python: str = sys.executable, read_only: bool = False):
        self.id = run_id
        self.scenario = scenario
        self.ticket = ticket
        self.source_dir = source_dir      # the user's working folder (never edited by the run itself)
        self.files = files                # snapshot of the folder taken when the run started
        self.python = python              # interpreter that runs the frozen tests
        self.read_only = read_only        # demo folders: verified changes cannot be applied back
        self.budget = budget
        self.node_caps = node_caps
        self.lock = threading.Lock()
        self.git_lock = threading.RLock()
        self.calls_used = 0
        self.tokens_used = 0
        self.started = time.monotonic()
        self.cancelled = False
        self.seen_urls: dict[str, dict] = {}   # URLs returned by web_search in this run
        self.triage: dict = {}
        self.criteria: list[dict] = []
        self.test_ids: list[str] = []
        self.live_test_ids: list[str] = []
        self.frozen_files: list[str] = []
        self.frozen_hash: Optional[str] = None
        self.best_attempt: Optional[dict] = None
        self.root_id = new_id("n")
        self.sandbox = Sandbox(self)

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def exhausted_reason(self) -> str:
        b = self.budget
        if self.calls_used >= b.max_llm_calls:
            return f"run LLM calls {self.calls_used}/{b.max_llm_calls}"
        if self.tokens_used >= b.max_total_tokens:
            return f"run tokens {self.tokens_used}/{b.max_total_tokens}"
        if self.elapsed() >= b.max_wall_seconds:
            return f"wall time {int(self.elapsed())}s/{b.max_wall_seconds}s"
        return ""

    def exhausted(self) -> bool:
        return bool(self.exhausted_reason())

    def check_cancel(self) -> None:
        if self.cancelled:
            raise RunCancelled("run cancelled")

    def charge(self, nb: NodeBudget) -> None:
        """Every LLM call decrements the node budget and the run budget atomically."""
        with self.lock:
            if self.cancelled:
                raise RunCancelled("run cancelled")
            why = self.exhausted_reason()
            if not why and nb.llm_calls <= 0:
                why = "node LLM call slice used up"
            if why:
                raise BudgetExhausted(why)
            self.calls_used += 1
            nb.llm_calls -= 1

    def add_tokens(self, n: int) -> None:
        with self.lock:
            self.tokens_used += n

    def carve(self, nb: NodeBudget, n_children: int, fraction: float = 1.0) -> int:
        """Take a per-child slice out of the parent's remaining calls, keeping the parent reserve."""
        with self.lock:
            avail = max(0, nb.llm_calls - config.PARENT_RESERVE_CALLS)
            per = int((avail * fraction) // max(1, n_children))
            nb.llm_calls -= per * n_children
            return per

    def refund(self, nb: NodeBudget, child_budgets: list[NodeBudget]) -> None:
        with self.lock:
            nb.llm_calls += sum(max(0, c.llm_calls) for c in child_budgets)


ACTIVE_RUNS: dict[str, RunState] = {}
ACTIVE_LOCK = threading.Lock()


@dataclass
class NodeCtx:
    """Everything a node's LLM calls and tools need: its run, budget slice and worktree."""
    run: RunState
    node_id: str
    depth: int
    budget: NodeBudget
    wt: Path
    base: str
