"""Unified-diff parsing, lenient application, and safety validation (F6)."""
import posixpath
import re
from pathlib import Path
from typing import Optional

from . import config
from .errors import PatchError
from .util import secret_values


def safe_rel(path: str) -> str:
    """Normalise a repo-relative path; reject absolute paths, escapes and .git."""
    p = (path or "").replace("\\", "/").strip()
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise ValueError(f"invalid relative path: {path!r}")
    norm = posixpath.normpath(p)
    if norm.startswith("..") or norm == "." or "/.git/" in f"/{norm}/":
        raise ValueError(f"path escapes the repository: {path!r}")
    return norm


def is_test_or_config(rel: str) -> bool:
    """Files that are frozen at depth 0: everything under tests/, test_*.py / *_test.py and conftest.py anywhere,
    and root test config."""
    base = posixpath.basename(rel)
    return (rel == "tests" or rel.startswith("tests/") or "/tests/" in f"/{rel}" or base == "conftest.py"
            or (base.startswith("test_") and base.endswith(".py")) or base.endswith("_test.py")
            or rel in config.FROZEN_CONFIG_NAMES)


def is_frozen_path(run, rel: str) -> bool:
    rel = rel[2:] if rel.startswith("./") else rel
    return rel in run.frozen_files or is_test_or_config(rel)


# ---------------------------------------------------------------------------
# Parsing and lenient application
# ---------------------------------------------------------------------------


def _diff_path(s: str) -> Optional[str]:
    s = s.split("\t")[0].strip()
    if s == "/dev/null":
        return None
    if s.startswith(("a/", "b/")):
        s = s[2:]
    return s


def parse_diff(diff: str) -> list[dict]:
    """Returns [{"old": path|None, "new": path|None, "hunks": [[" ctx", "-old", "+new", ...], ...]}]."""
    files: list[dict] = []
    cur: Optional[dict] = None
    hunk: Optional[list[str]] = None
    lines = diff.replace("\r\n", "\n").rstrip("\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            cur = {"old": _diff_path(line[4:]), "new": _diff_path(lines[i + 1][4:]), "hunks": []}
            files.append(cur)
            hunk = None
            i += 2
            continue
        if line.startswith("@@"):
            if cur is None:
                raise PatchError("hunk without a file header")
            hunk = []
            cur["hunks"].append(hunk)
        elif line.startswith("diff --git "):
            hunk = None
        elif hunk is not None and line[:1] in (" ", "+", "-"):
            hunk.append(line)
        elif hunk is not None and line == "":
            hunk.append(" ")  # blank context line whose leading space was dropped
        elif line.startswith("\\"):
            pass
        else:
            hunk = None
        i += 1
    return files


_SHIFTED_CHANGE_RE = re.compile(r"^ [-+](?![-+])")


def repair_shifted_markers(diff: str) -> str:
    """Some models indent change lines by one space inside hunks (" -old" / " +new"), which makes them context
    lines that never match. Turn them back into change lines. Only used after normal application failed."""
    out, in_hunk = [], False
    for line in diff.split("\n"):
        if line.startswith("@@"):
            in_hunk = True
        elif line.startswith(("diff --git ", "--- ", "+++ ")):
            in_hunk = False
        elif in_hunk and _SHIFTED_CHANGE_RE.match(line):
            line = line[1:]
        out.append(line)
    return "\n".join(out)


def _find_block(lines: list[str], block: list[str], start: int) -> Optional[int]:
    """Find block in lines, searching from start first; exact, then ignoring trailing, then all whitespace."""
    n = len(block)
    if n == 0 or n > len(lines):
        return None
    order = list(range(start, len(lines) - n + 1)) + list(range(0, min(start, len(lines) - n + 1)))
    for norm in (lambda s: s, lambda s: s.rstrip(), lambda s: s.strip()):
        target = [norm(b) for b in block]
        for idx in order:
            if [norm(x) for x in lines[idx:idx + n]] == target:
                return idx
    return None


def lenient_apply(run, wt: Path, diff: str) -> None:
    """Apply hunks by matching their context text, ignoring line numbers. Raises PatchError on any mismatch."""
    files = parse_diff(diff)
    if not files:
        raise PatchError("no file headers found in diff")
    root = wt.resolve()
    for fp in files:
        rel = safe_rel(fp["new"] or fp["old"])
        if is_frozen_path(run, rel):
            raise PatchError(f"refusing to modify frozen file {rel}")
        target = (wt / rel).resolve()
        if root not in target.parents:
            raise PatchError(f"path outside worktree: {rel}")
        if fp["old"] is None:
            new_lines = [l[1:] for h in fp["hunks"] for l in h if l[:1] in (" ", "+")]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            continue
        src = (wt / safe_rel(fp["old"])).resolve()
        if not src.is_file():
            raise PatchError(f"file not found: {fp['old']}")
        if fp["new"] is None:
            src.unlink()
            continue
        text = src.read_text(encoding="utf-8", errors="replace")
        had_nl = text.endswith("\n")
        lines = text.split("\n")
        if had_nl:
            lines = lines[:-1]
        pos = 0
        for k, h in enumerate(fp["hunks"], 1):
            old_b = [l[1:] for l in h if l[:1] in (" ", "-")]
            new_b = [l[1:] for l in h if l[:1] in (" ", "+")]
            if not old_b:
                lines.extend(new_b)
                pos = len(lines)
                continue
            idx = _find_block(lines, old_b, pos)
            if idx is None:
                while old_b and old_b[-1].strip() == "" and new_b and new_b[-1].strip() == "":
                    old_b, new_b = old_b[:-1], new_b[:-1]
                idx = _find_block(lines, old_b, pos)
            if idx is None:
                raise PatchError(f"hunk {k} does not match the current content of {rel}")
            lines[idx:idx + len(old_b)] = new_b
            pos = idx + len(new_b)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + ("\n" if had_nl or lines else ""), encoding="utf-8")
        if target != src and src.exists():
            src.unlink()


# ---------------------------------------------------------------------------
# Validation (F6)
# ---------------------------------------------------------------------------

_HEADER_PATH_RES = [
    re.compile(r"^diff --git a/(\S+) b/(\S+)"),
    re.compile(r"^(?:---|\+\+\+) (\S+)"),
    re.compile(r"^(?:rename|copy) (?:from|to) (\S+)"),
]
_FORBIDDEN_ADDED_RE = re.compile(r"pytest\.skip|xfail|assert\s+True\b")
MAX_CHANGED_LINES = 400


def _patch_paths(diff: str) -> set[str]:
    paths: set[str] = set()
    for line in diff.splitlines():
        for rx in _HEADER_PATH_RES:
            m = rx.match(line)
            if m:
                paths.update(p for p in (_diff_path(g) for g in m.groups()) if p)
    return paths


def validate_patch(run, diff: str, wt: Path, base: str) -> tuple[bool, str]:
    """Reject patches that touch frozen files or .git, escape the worktree, contain secrets, delete >50% of a file,
    exceed 400 changed lines, or add pytest.skip / xfail / assert True."""
    if not diff or not diff.strip():
        return False, "empty patch"
    paths = _patch_paths(diff)
    if not paths:
        return False, "patch has no file headers"
    root = wt.resolve()
    for p in paths:
        if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
            return False, f"absolute path in patch: {p}"
        norm = posixpath.normpath(p)
        if norm == ".git" or norm.startswith(".git/") or "/.git/" in f"/{norm}/":
            return False, f"patch touches .git: {p}"
        target = (wt / norm).resolve()
        if norm.startswith("..") or (target != root and root not in target.parents):
            return False, f"patch adds or edits a file outside the worktree: {p}"
        if is_frozen_path(run, norm):
            return False, f"patch touches frozen test/config file: {norm}"
    for s in secret_values():
        if s in diff:
            return False, "patch contains a configured secret value"
    try:
        files = parse_diff(diff)
    except PatchError as e:
        return False, f"unparseable diff: {e}"
    total_changed = 0
    for fp in files:
        added = [l[1:] for h in fp["hunks"] for l in h if l.startswith("+")]
        removed = [l for h in fp["hunks"] for l in h if l.startswith("-")]
        total_changed += len(added) + len(removed)
        for a in added:
            if _FORBIDDEN_ADDED_RE.search(a):
                return False, f"patch adds a forbidden test-weakening construct: {a.strip()[:120]}"
        if fp["old"] and fp["new"] is None:
            return False, f"patch deletes file {fp['old']}"
        if fp["old"]:
            orig = run.sandbox.orig_lines(wt, base, posixpath.normpath(fp["old"]))
            if orig and (len(removed) - len(added)) > 0.5 * orig:
                return False, f"patch deletes more than 50% of {fp['old']}"
    if total_changed > MAX_CHANGED_LINES:
        return False, f"patch too large: {total_changed} changed lines (max {MAX_CHANGED_LINES})"
    return True, "ok"
