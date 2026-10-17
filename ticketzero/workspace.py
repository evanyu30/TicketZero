"""The user's working folder: what the agent may see, which Python runs the tests, and applying results back.

Runs never edit the folder directly. It is copied into the run sandbox (one git worktree per node, F3), and a
verified change is written back only when the user asks for it (POST /runs/{id}/apply), and only to files that
have not changed since the run started.
"""
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config
from .patches import is_test_or_config, parse_diff, safe_rel

# Never copied into the sandbox, so never readable by the agents or sent to a model.
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
             ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".idea", ".vscode", "dist", "build", ".next",
             "ticketzero_data", ".ipynb_checkpoints", "_answer_key"}
SECRET_FILE_RE = re.compile(r"^\.env(\..+)?$|\.(pem|key|p12|pfx|keystore|jks)$|^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$"
                            r"|^\.netrc$|^\.pypirc$|credentials(\.json)?$", re.I)
SECRET_ALLOW = {".env.example", ".env.sample", ".env.template"}
MAX_FILES = 3000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 30 * 1024 * 1024
VENV_CANDIDATES = (".venv", "venv", "env")


class FolderError(ValueError):
    """The folder cannot be used for a run (missing, too large, no tests, ...)."""


@dataclass
class FolderScan:
    root: Path
    files: dict[str, bytes]
    test_files: list[str]
    is_git: bool
    python: str
    python_label: str
    skipped_secrets: list[str] = field(default_factory=list)
    skipped_large: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {"path": str(self.root), "files": len(self.files), "test_files": self.test_files[:50],
                "test_file_count": len(self.test_files), "is_git": self.is_git, "python": self.python_label,
                "skipped_secrets": self.skipped_secrets[:20], "skipped_large": self.skipped_large[:20],
                "total_bytes": sum(len(b) for b in self.files.values())}


def _is_secret(rel: str) -> bool:
    name = Path(rel).name
    return name not in SECRET_ALLOW and bool(SECRET_FILE_RE.search(name))


def _git_listing(root: Path) -> Optional[list[str]]:
    """Tracked + untracked-but-not-ignored files, relative to root, when root is inside a git work tree."""
    try:
        p = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return [r for r in p.stdout.decode("utf-8", "replace").split("\0") if r]


def _walk_listing(root: Path) -> list[str]:
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for name in sorted(files):
            out.append(Path(dirpath, name).relative_to(root).as_posix())
            if len(out) > MAX_FILES * 2:
                return out
    return out


def detect_python(root: Path) -> tuple[str, str]:
    """Prefer the project's own virtualenv (if it has pytest), so tests run with the project's dependencies."""
    for name in VENV_CANDIDATES:
        py = root / name / "bin" / "python"
        if py.is_file():
            try:
                ok = subprocess.run([str(py), "-c", "import pytest"], capture_output=True, timeout=30).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ok = False
            if ok:
                return str(py), f"{name}/bin/python (project virtualenv)"
    return sys.executable, "TicketZero's Python (no project virtualenv with pytest found)"


def scan_folder(path: str) -> FolderScan:
    """Read every file the agent may see. Raises FolderError with a user-facing message when unusable."""
    if not path or not path.strip():
        raise FolderError("Choose a folder first.")
    root = Path(path.strip()).expanduser().resolve()
    if not root.is_dir():
        raise FolderError(f"Folder not found: {root}")
    home = config.HOME.resolve()
    if root == home or home in root.parents:
        raise FolderError("That folder is TicketZero's own data folder.")
    listing = _git_listing(root)
    is_git = listing is not None
    if listing is None:
        listing = _walk_listing(root)
    files: dict[str, bytes] = {}
    secrets, large = [], []
    total = 0
    for rel in listing:
        parts = rel.split("/")
        if any(p in SKIP_DIRS or p.endswith(".egg-info") for p in parts[:-1]):
            continue
        f = root / rel
        if f.is_symlink() or not f.is_file():
            continue            # symlinks could point outside the folder
        if (root / parts[0]).resolve() == home:
            continue
        if _is_secret(rel):
            secrets.append(rel)
            continue
        size = f.stat().st_size
        if size > MAX_FILE_BYTES:
            large.append(rel)
            continue
        total += size
        if len(files) >= MAX_FILES or total > MAX_TOTAL_BYTES:
            raise FolderError(f"Folder is too large (limit {MAX_FILES} files / {MAX_TOTAL_BYTES // 2**20} MB). "
                              "Choose the project folder itself, or a smaller reproduction.")
        files[safe_rel(rel)] = f.read_bytes()
    if not files:
        raise FolderError("The folder has no readable files.")
    test_files = sorted(r for r in files if is_test_or_config(r) and r.endswith(".py"))
    python, label = detect_python(root)
    return FolderScan(root, files, test_files, is_git, python, label, secrets, large)


def require_tests(scan: FolderScan) -> None:
    if not scan.test_files:
        raise FolderError("No pytest tests found (expected tests/ or test_*.py files). TicketZero verifies every "
                          "fix against tests, so add at least one test that fails now and should pass when fixed.")


# ---------------------------------------------------------------------------
# Writing a verified result back
# ---------------------------------------------------------------------------


def _git_show_bytes(repo: Path, spec: str) -> Optional[bytes]:
    p = subprocess.run(["git", "-C", str(repo), "show", spec], capture_output=True, timeout=30)
    return p.stdout if p.returncode == 0 else None


def apply_to_folder(source_dir: Path, sandbox_base: Path, patch: str) -> list[str]:
    """Write the verified versions of the patched files into the user's folder.

    Refuses (FolderError) if any of those files changed in the folder since the run started, so the user's own
    edits are never overwritten.
    """
    root = source_dir.resolve()
    first = subprocess.run(["git", "-C", str(sandbox_base), "rev-list", "--max-parents=0", "HEAD"],
                           capture_output=True, text=True, timeout=30)
    if first.returncode != 0:
        raise FolderError("The run's sandbox is missing; it may have been deleted.")
    base_commit = first.stdout.split()[0]
    paths = sorted({safe_rel(p) for fp in parse_diff(patch) for p in (fp["old"], fp["new"]) if p})
    if not paths:
        raise FolderError("The run produced no file changes.")
    changed = []
    for rel in paths:
        target = (root / rel).resolve()
        if root not in target.parents:
            raise FolderError(f"Refusing to write outside the folder: {rel}")
        original = _git_show_bytes(sandbox_base, f"{base_commit}:{rel}")
        current = target.read_bytes() if target.is_file() else None
        if current != original:
            changed.append(rel)
    if changed:
        raise FolderError("These files changed in your folder after the run started, so nothing was applied: "
                          + ", ".join(changed))
    written = []
    for rel in paths:
        target = root / rel
        final = sandbox_base / rel
        if final.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(final.read_bytes())
        elif target.exists():
            target.unlink()
        written.append(rel)
    return written
