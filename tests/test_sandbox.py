"""Frozen tests, patch validation and lenient application on a real Scenario A sandbox (F6)."""
import difflib

import pytest

from fake_nemotron_env import SECRET
from ticketzero.budget import RunBudget, RunState
from ticketzero.scenarios import SCENARIOS
from ticketzero.patches import validate_patch
from ticketzero.util import new_id, redact
from ticketzero.verify import collect_tests, run_pytest
from ticketzero.workspace import scan_folder

CFG = (SCENARIOS["multi_bug_chat_service"]["folder"] / "app/config.py").read_text()


def udiff(path, old, new, old_path=None):
    body = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        old_path or f"a/{path}", f"b/{path}"))
    return f"diff --git a/{path} b/{path}\n" + body


@pytest.fixture()
def sandbox(client):  # client fixture opens the store
    sc = SCENARIOS["multi_bug_chat_service"]
    scan = scan_folder(str(sc["folder"]))
    run = RunState(new_id("rtest"), "A", sc["ticket"], scan.root, scan.files, RunBudget(), {}, python=scan.python)
    base = run.sandbox.init_base()
    run.sandbox.freeze(run.sandbox.base)
    run.test_ids = collect_tests(run, run.sandbox.base)
    return run, run.sandbox.base, base


REJECTED = {
    "tests/": ("--- a/tests/test_config.py\n+++ b/tests/test_config.py\n@@ -1 +1 @@\n-from app import config\n"
               "+from app import config  # x\n", "frozen"),
    "pytest.ini": ("--- a/pytest.ini\n+++ b/pytest.ini\n@@ -1 +1 @@\n-[pytest]\n+[pytest] \n", "frozen"),
    "conftest": ("--- /dev/null\n+++ b/conftest.py\n@@ -0,0 +1 @@\n+import os\n", "frozen"),
    ".git": ("--- a/.git/config\n+++ b/.git/config\n@@ -1 +1 @@\n-a\n+b\n", ".git"),
    "escape": ("--- a/../evil.py\n+++ b/../evil.py\n@@ -0,0 +1 @@\n+x=1\n", "outside the worktree"),
    "absolute": ("--- /dev/null\n+++ b//etc/evil\n@@ -0,0 +1 @@\n+x\n", "absolute"),
    "secret": (udiff("app/config.py", CFG, CFG.replace('API_KEY_ENV = "CRUSOE_API_KEY"', f'K = "{SECRET}"')),
               "secret"),
    "skip": (udiff("app/config.py", CFG, CFG + "import pytest\npytest.skip('no')\n"), "forbidden"),
    "xfail": (udiff("app/config.py", CFG, CFG + "# xfail\n"), "forbidden"),
    "assert True": (udiff("app/config.py", CFG, CFG + "assert True\n"), "forbidden"),
    "deletes >50%": (udiff("app/config.py", CFG, "\n".join(CFG.splitlines()[:5]) + "\n"), "more than 50%"),
    "deletes file": ("diff --git a/app/config.py b/app/config.py\n--- a/app/config.py\n+++ /dev/null\n"
                     "@@ -1,2 +0,0 @@\n-a\n-b\n", "deletes file"),
    "too large": (udiff("app/new.py", "", "".join(f"x{i} = {i}\n" for i in range(450)), "/dev/null"), "too large"),
}


@pytest.mark.parametrize("name", REJECTED)
def test_validator_rejects(sandbox, name):
    run, wt, base = sandbox
    diff, expected = REJECTED[name]
    ok, reason = validate_patch(run, diff, wt, base)
    assert not ok and expected in reason


def test_validator_accepts_minimal_fix(sandbox):
    run, wt, base = sandbox
    assert validate_patch(run, udiff("app/config.py", CFG, CFG.replace("= 64", "= 4096")), wt, base) == (True, "ok")


def test_frozen_files_are_read_only_and_hashed(sandbox):
    run, wt, _ = sandbox
    assert run.frozen_files == ["pytest.ini", "tests/test_config.py", "tests/test_live.py"]
    assert all((wt / f).stat().st_mode & 0o777 == 0o444 for f in run.frozen_files)
    child, _ = run.sandbox.create_worktree(wt, "nchild")
    assert all((child / f).stat().st_mode & 0o777 == 0o444 for f in run.frozen_files)


def test_tamper_detection(sandbox):
    run, wt, base = sandbox
    assert run.sandbox.tamper_check(wt) == (True, "ok")
    f = wt / "tests/test_config.py"
    f.chmod(0o644)
    f.write_text(f.read_text() + "\n# tampered\n")
    assert "hash mismatch" in run.sandbox.tamper_check(wt)[1]
    run.sandbox.reset_hard(wt, base)
    (wt / "tests/test_extra.py").write_text("def test_x(): pass\n")
    assert "unexpected test" in run.sandbox.tamper_check(wt)[1]


def test_lenient_apply_handles_sloppy_llm_diff_and_rolls_back_bad_one(sandbox):
    run, wt, base = sandbox
    sloppy = ("--- a/app/config.py\n+++ b/app/config.py\n@@ -99,3 +99,3 @@\n"
              " # Chat model served for the support bot.\n"
              " # Must be a model that is currently available on Crusoe.\n"
              '-MODEL = "meta-llama/Llama-3.3-70B-Instruct"\n+MODEL = "nvidia/Nemotron-3-Super-120B-A12B"\n\n')
    assert run.sandbox.apply(wt, sloppy) == (True, "")
    bad = "--- a/app/config.py\n+++ b/app/config.py\n@@ -1 +1 @@\n-THIS LINE DOES NOT EXIST\n+x\n"
    assert run.sandbox.apply(wt, bad)[0] is False
    diff = run.sandbox.diff(wt, base)
    assert '+MODEL = "nvidia/Nemotron-3-Super-120B-A12B"' in diff and "THIS LINE" not in diff


def test_unmodified_repo_fails_frozen_suite(sandbox):
    run, wt, _ = sandbox
    r = run_pytest(run, wt)
    assert not r["passed"] and r["counts"]["failed"] == 3


def test_redaction():
    text = f"Authorization: Bearer {SECRET} and Bearer abcdefghijklmnopqrstu and {SECRET}"
    assert redact(text) == "Authorization: Bearer [REDACTED] and Bearer [REDACTED] and [REDACTED]"


def test_repairs_diff_with_indented_change_lines(sandbox):
    """Shape of a real Lightning fixer patch: change lines indented by one space (" -old", " +new")."""
    run, wt, base = sandbox
    shifted = ("--- a/app/config.py\n+++ b/app/config.py\n@@ -5,7 +5,7 @@\n"
               " # Crusoe Managed Inference endpoint (OpenAI-compatible).\n"
               " # The client appends /chat/completions to this URL.\n"
               ' -BASE_URL = "https://api.crusoe.ai/v1"\n'
               ' +BASE_URL = "https://api.inference.crusoecloud.com/v1"\n'
               " \n"
               " # Chat model served for the support bot.\n")
    ok, err = run.sandbox.apply(wt, shifted)
    assert ok, err
    diff = run.sandbox.diff(wt, base)
    assert '+BASE_URL = "https://api.inference.crusoecloud.com/v1"' in diff
    assert '-BASE_URL = "https://api.crusoe.ai/v1"' in diff
    assert validate_patch(run, diff, wt, base) == (True, "ok")
