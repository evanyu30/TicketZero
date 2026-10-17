"""Per-run sandbox: git base repo, one worktree per node (F3), frozen tests (F6), patch application.

Layout: TICKETZERO_HOME/runs/<run_id>/{base, wt/<node_id>, patches}. Nothing outside TICKETZERO_HOME is written.
"""
import hashlib
import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

from . import config
from .errors import GitError, PatchError
from .patches import is_test_or_config, lenient_apply, repair_shifted_markers, safe_rel
from .util import trunc

GIT_FLAGS = ["-c", "user.name=TicketZero", "-c", "user.email=ticketzero@localhost", "-c", "commit.gpgsign=false",
             "-c", f"core.hooksPath={os.devnull}", "-c", "advice.detachedHead=false"]
# Kept out of every diff via .git/info/exclude (shared by all worktrees), so the user's .gitignore is untouched.
GIT_EXCLUDE = "__pycache__/\n*.pyc\n.pytest_cache/\ntests/*.wav\n"
# Written when the folder has no pytest config, so pytest never picks up a config from a parent directory.
DEFAULT_PYTEST_INI = "[pytest]\nmarkers =\n    live: requires network access to Crusoe Managed Inference\n"
PYTEST_CONFIGS = ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")


def sandbox_home() -> Path:
    """HOME for git and pytest subprocesses, kept inside TICKETZERO_HOME."""
    p = config.HOME / "sandbox_home"
    p.mkdir(parents=True, exist_ok=True)
    return p


def git(cwd: Path, *args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(sandbox_home()), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "LANG": "C"}
    p = subprocess.run(["git", *GIT_FLAGS, *args], cwd=str(cwd), env=env, capture_output=True, text=True,
                       errors="replace", timeout=timeout)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])} failed: {trunc(p.stderr.strip() or p.stdout.strip(), 600)}")
    return p


class Sandbox:
    def __init__(self, run):
        self.run = run
        self.run_dir = config.HOME / "runs" / run.id
        self.base = self.run_dir / "base"
        self.wt_root = self.run_dir / "wt"
        self.patch_dir = self.run_dir / "patches"
        self.base_commit = ""

    def init_base(self) -> str:
        """runs/<run_id>/base: a copy of the working folder snapshot, git init + initial commit."""
        for d in (self.base, self.wt_root, self.patch_dir):
            d.mkdir(parents=True, exist_ok=True)
        files = dict(self.run.files)
        if not any(name in files for name in PYTEST_CONFIGS):
            files["pytest.ini"] = DEFAULT_PYTEST_INI.encode()
        for rel, content in files.items():
            target = self.base / safe_rel(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        with self.run.git_lock:
            git(self.base, "init", "-q", "-b", "main")
            (self.base / ".git" / "info").mkdir(parents=True, exist_ok=True)
            (self.base / ".git" / "info" / "exclude").write_text(GIT_EXCLUDE, encoding="utf-8")
            git(self.base, "add", "-A")
            git(self.base, "commit", "-q", "--no-verify", "-m", "Initial scenario repo")
            self.base_commit = git(self.base, "rev-parse", "HEAD").stdout.strip()
        return self.base_commit

    # -- F6: frozen tests --
    def freeze(self, wt: Path) -> str:
        """Hash every test/config file and make them read-only. Returns the frozen hash."""
        with self.run.git_lock:
            tracked = git(wt, "ls-files").stdout.splitlines()
        self.run.frozen_files = sorted(p for p in tracked if is_test_or_config(p))
        self.run.frozen_hash = self.compute_hash(wt)
        self.protect(wt)
        return self.run.frozen_hash

    def compute_hash(self, wt: Path) -> str:
        h = hashlib.sha256()
        for rel in self.run.frozen_files:
            f = wt / rel
            digest = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else "MISSING"
            h.update(f"{rel}\0{digest}\n".encode())
        return h.hexdigest()

    def tamper_check(self, wt: Path) -> tuple[bool, str]:
        """Frozen hash must match, and no new test or config files may appear."""
        current = self.compute_hash(wt)
        if current != self.run.frozen_hash:
            return False, f"frozen hash mismatch: expected {self.run.frozen_hash}, got {current}"
        frozen = set(self.run.frozen_files)
        for root, dirs, files in os.walk(wt):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for name in files:
                rel = Path(root, name).relative_to(wt).as_posix()
                if rel in frozen:
                    continue
                if is_test_or_config(rel) and (rel.endswith(".py") or rel in config.FROZEN_CONFIG_NAMES):
                    return False, f"unexpected test or configuration file added: {rel}"
        return True, "ok"

    def protect(self, wt: Path) -> None:
        for rel in self.run.frozen_files:
            f = wt / rel
            if f.is_file():
                f.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    # -- F3: one worktree per node --
    def snapshot(self, wt: Path, message: str) -> str:
        with self.run.git_lock:
            git(wt, "add", "-A")
            git(wt, "commit", "-q", "--no-verify", "--allow-empty", "-m", message)
            return git(wt, "rev-parse", "HEAD").stdout.strip()

    def create_worktree(self, parent_wt: Path, node_id: str) -> tuple[Path, str]:
        """Commit the parent's current state, then `git worktree add` a branch for the child at that commit.
        Falls back to copytree + git init if worktrees fail."""
        path = self.wt_root / node_id
        with self.run.git_lock:
            commit = self.snapshot(parent_wt, f"snapshot before child {node_id}")
            try:
                git(self.base, "worktree", "add", "-q", str(path), "-b", f"node-{node_id}", commit)
            except GitError:
                if path.exists():
                    shutil.rmtree(path, ignore_errors=True)
                shutil.copytree(parent_wt, path, ignore=shutil.ignore_patterns(".git", "__pycache__"))
                for f in path.rglob("*"):
                    if f.is_file():
                        f.chmod(f.stat().st_mode | stat.S_IWUSR)
                git(path, "init", "-q", "-b", "main")
                (path / ".git" / "info").mkdir(parents=True, exist_ok=True)
                (path / ".git" / "info" / "exclude").write_text(GIT_EXCLUDE, encoding="utf-8")
                git(path, "add", "-A")
                git(path, "commit", "-q", "--no-verify", "-m", f"copy of parent for {node_id}")
                commit = git(path, "rev-parse", "HEAD").stdout.strip()
        self.protect(path)
        return path, commit

    def diff(self, wt: Path, base: str) -> str:
        """Everything in the worktree (including new files) relative to the node's base commit."""
        with self.run.git_lock:
            git(wt, "add", "-A")
            return git(wt, "diff", "--cached", "--no-color", "--no-ext-diff", base).stdout

    def reset_hard(self, wt: Path, commit: str) -> None:
        with self.run.git_lock:
            git(wt, "reset", "-q", "--hard", commit)
            git(wt, "clean", "-q", "-fd")
        self.protect(wt)

    def orig_lines(self, wt: Path, base: str, rel: str) -> int:
        p = git(wt, "show", f"{base}:{rel}", check=False)
        return len(p.stdout.splitlines()) if p.returncode == 0 else 0

    def apply(self, wt: Path, patch: str) -> tuple[bool, str]:
        """Apply a patch: git apply (recount), then 3-way, then a lenient context matcher. Restores on failure."""
        if not patch.strip():
            return False, "empty patch"
        pf = self.patch_dir / f"{uuid.uuid4().hex}.diff"
        pf.write_text(patch if patch.endswith("\n") else patch + "\n", encoding="utf-8")
        errors = []
        with self.run.git_lock:
            self.snapshot(wt, "pre-apply snapshot")
            for args in (["apply", "--recount", "--whitespace=nowarn"], ["apply", "--3way", "--whitespace=nowarn"]):
                p = git(wt, *args, str(pf), check=False)
                if p.returncode == 0:
                    self.protect(wt)
                    return True, ""
                errors.append(f"git {' '.join(args)}: {trunc(p.stderr.strip(), 300)}")
                self._restore_head(wt)
            candidates = [("lenient apply", patch)]
            repaired = repair_shifted_markers(patch)
            if repaired != patch:
                candidates.append(("lenient apply after repairing indented +/- lines", repaired))
            for label, text in candidates:
                try:
                    lenient_apply(self.run, wt, text)
                    self.protect(wt)
                    return True, ""
                except (PatchError, OSError, ValueError) as e:
                    errors.append(f"{label}: {e}")
                    self._restore_head(wt)
        self.protect(wt)
        return False, " | ".join(errors)

    def _restore_head(self, wt: Path) -> None:
        git(wt, "reset", "-q", "--hard", "HEAD")
        git(wt, "clean", "-q", "-fd")
