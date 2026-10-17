"""Frozen test runner and the Independent Verifier (F6)."""
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from . import config
from .budget import NodeCtx
from .errors import NodeFailed
from .llm import json_call
from .models import REGISTRY
from .prompts import VERIFIER_SYSTEM
from .sandbox import sandbox_home
from .schemas import CriticOut
from .store import store
from .util import now_iso, redact, tail, trunc

_RESULT_RE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+)")
_COUNT_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)")


def pytest_env(wt: Path) -> dict:
    """The only environment the test subprocess sees."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(sandbox_home()), "PYTHONPATH": str(wt),
            "CRUSOE_API_KEY": config.CRUSOE_API_KEY}


def collect_tests(run, wt: Path, marker: Optional[str] = None) -> list[str]:
    """Test IDs pytest discovers in the worktree (honouring the project's own pytest config)."""
    cmd = [run.python, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
    if marker:
        cmd += ["-m", marker]
    p = subprocess.run(cmd, cwd=str(wt), env=pytest_env(wt), capture_output=True, text=True, errors="replace",
                       timeout=120)
    return [l.strip() for l in p.stdout.splitlines() if "::" in l and not l.startswith(" ")]


_TEST_DEF_RE = re.compile(r"^([ \t]*)(?:async[ \t]+)?def[ \t]+(test_\w+)[ \t]*\(|^class[ \t]+(Test\w+)", re.M)


def static_test_ids(wt: Path, frozen_files: list[str]) -> list[str]:
    """Test IDs read from the frozen test files without importing them. Used when pytest cannot collect (for
    example the app under test raises at import), so criteria and per-test results still name every test."""
    ids: list[str] = []
    for rel in frozen_files:
        name = Path(rel).name
        if not rel.endswith(".py") or name == "conftest.py" or not (name.startswith("test_") or name.endswith("_test.py")):
            continue
        try:
            text = (wt / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        cls = None
        for indent, func, klass in _TEST_DEF_RE.findall(text):
            if klass:
                cls = klass
            elif not indent:
                cls = None
                ids.append(f"{rel}::{func}")
            elif cls:
                ids.append(f"{rel}::{cls}::{func}")
    return ids


def run_pytest(run, wt: Path, test_ids: Optional[list[str]] = None) -> dict:
    """Run the frozen suite (or known test IDs) and return per-test outcomes plus the output tail.

    Live tests are slow (real network calls, up to the 180 s timeout against a broken endpoint), so when the
    selection includes them, offline tests run first and a failure there ends verification without the live run.
    """
    known_files = {x.split("::")[0] for x in run.test_ids}
    ids = [t for t in (test_ids or []) if t in run.test_ids or t.split("::")[0] in known_files]
    live = set(run.live_test_ids)
    includes_live = bool(live) and (not ids or any(t in live or any(l.startswith(t) for l in live) for t in ids))
    if config.LIVE_TESTS and includes_live:
        offline = _pytest(run, wt, ids, "not live")
        if offline["exit_code"] not in (0, 5):
            offline["summary"] += " (live tests not run: offline tests failed)"
            return offline
        return _pytest(run, wt, ids, None)
    return _pytest(run, wt, ids, None if config.LIVE_TESTS else "not live")


def _pytest(run, wt: Path, ids: list[str], marker: Optional[str]) -> dict:
    cmd = [run.python, "-m", "pytest", "-q", "-rA", "--color=no", "-p", "no:cacheprovider"]
    if marker:
        cmd += ["-m", marker]
    cmd += ids
    try:
        p = subprocess.run(cmd, cwd=str(wt), env=pytest_env(wt), capture_output=True, text=True, errors="replace",
                           timeout=config.PYTEST_TIMEOUT_SECONDS)
        out, code = (p.stdout or "") + (p.stderr or ""), p.returncode
    except subprocess.TimeoutExpired as e:
        out, code = f"pytest timed out after {config.PYTEST_TIMEOUT_SECONDS}s\n" + str(e.stdout or ""), -1
    results: dict[str, str] = {}
    for line in out.splitlines():
        m = _RESULT_RE.match(line)
        if m and "::" in m.group(2):
            results[m.group(2)] = m.group(1).lower()
    counts = {k: 0 for k in ("passed", "failed", "error", "skipped")}
    for v in results.values():
        if v in counts:
            counts[v] += 1
    summary_line = next((l for l in reversed(out.splitlines()) if _COUNT_RE.search(l) or "no tests ran" in l), "")
    for n, kind in _COUNT_RE.findall(summary_line):
        k = "error" if kind.startswith("error") else kind
        if k in counts:
            counts[k] = max(counts[k], int(n))
    # Skips count as not passing: a frozen test must actually run and pass.
    passed = (code == 0 and counts["passed"] > 0 and counts["failed"] == 0 and counts["error"] == 0
              and counts["skipped"] == 0)
    if code == 5:
        summary_line = summary_line or "no tests ran (all selected tests deselected or none collected)"
    return {
        "cmd": " ".join(cmd[1:]), "exit_code": code, "results": results, "counts": counts, "passed": passed,
        "summary": summary_line.strip("= ").strip() or f"exit code {code}",
        "output_tail": redact(tail(out, 3000)), "live_tests_enabled": config.LIVE_TESTS,
    }


def independent_verify(nc: NodeCtx, scope: Optional[list[str]], diff: str) -> dict:
    """Frozen-hash check, pytest in a subprocess, then a Super critic that can only veto a passing patch."""
    run = nc.run
    hash_ok, hash_detail = run.sandbox.tamper_check(nc.wt)
    proof: dict[str, Any] = {"scope": scope or "full_suite", "frozen_hash": run.frozen_hash, "hash_ok": hash_ok,
                             "hash_detail": hash_detail, "verified_at": now_iso()}
    if not hash_ok:
        proof.update(passed=False, reason="tests tampered", summary="tests tampered: " + hash_detail)
        store().event(run.id, nc.node_id, "verify_result", proof["summary"], proof)
        return proof
    tests = run_pytest(run, nc.wt, scope)
    proof["tests"] = tests
    passed = tests["passed"]
    reason = "" if passed else f"tests failed: {tests['summary']}"
    if passed and not diff.strip():
        passed, reason = False, "tests pass but no change was produced; cannot attribute the pass to a fix"
    critic = None
    if passed:
        critic = _critic(nc, diff, tests)
        if critic["veto"] is None:
            passed, reason = False, f"critic unavailable: {critic['reason']}"
        elif critic["veto"]:
            passed, reason = False, f"critic veto: {critic['reason']}"
    proof["critic"] = critic
    proof["passed"] = passed
    proof["reason"] = reason or "all selected frozen tests passed and the critic did not veto"
    proof["summary"] = ("PASSED: " if passed else "FAILED: ") + (reason or tests["summary"])
    _track_best_attempt(nc, diff, proof, tests)
    store().event(run.id, nc.node_id, "verify_result", proof["summary"], proof)
    return proof


def _critic(nc: NodeCtx, diff: str, tests: dict) -> dict:
    """LLM critic on the diff and test output only. veto=None means no valid verdict could be obtained."""
    user = ("## Diff\n```diff\n" + trunc(diff, 30000) + "\n```\n\n## Frozen test output (passed)\n```\n"
            + tests["output_tail"] + "\n```")
    base = {"model": REGISTRY.model_for("verifier"), "fixer_model": REGISTRY.model_for("fixer")}
    try:
        verdict = json_call(nc, "verifier", [{"role": "system", "content": VERIFIER_SYSTEM},
                                             {"role": "user", "content": user}], CriticOut, thinking=True)
        return {**base, "veto": verdict.veto, "reason": verdict.reason}
    except NodeFailed as e:
        return {**base, "veto": None, "reason": str(e)}


def _track_best_attempt(nc: NodeCtx, diff: str, proof: dict, tests: dict) -> None:
    """Remember the patch that passed the most tests, for the escalation bundle."""
    run = nc.run
    n_pass = sum(1 for v in tests["results"].values() if v == "passed")
    with run.lock:
        if diff.strip() and (run.best_attempt is None or n_pass >= run.best_attempt["n_pass"]):
            run.best_attempt = {"n_pass": n_pass, "patch": diff, "proof": proof, "node_id": nc.node_id}
