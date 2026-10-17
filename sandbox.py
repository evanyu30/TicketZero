"""
Path guardrail for TicketZero's file tools.

Every agent tool that reads, searches or writes files should pass its path
through safe_path(). Anything outside the ticket's workdir (for example
../../_answer_key/answers.json or build_catalog.py) is refused, and the
refusal is a normal tool error the agent sees and must work around.

    from sandbox import safe_path, safe_read, safe_write, SandboxViolation

    def read_repo_file(workdir, path):
        return safe_read(workdir, path)
"""
from pathlib import Path

BLOCKED_NAMES = {"_answer_key", "answers.json", "build_catalog.py", "score.py"}
READ_ONLY = {"test_app.py"}


class SandboxViolation(PermissionError):
    pass


def safe_path(workdir, path):
    root = Path(workdir).resolve()
    target = (root / path).resolve()  # resolves ../ and symlinks
    if target != root and root not in target.parents:
        raise SandboxViolation(f"Blocked: {path} is outside the ticket folder")
    if BLOCKED_NAMES & set(target.parts):
        raise SandboxViolation(f"Blocked: {path} is off-limits to the agent")
    return target


def safe_read(workdir, path):
    return safe_path(workdir, path).read_text()


def safe_write(workdir, path, content):
    target = safe_path(workdir, path)
    if target.name in READ_ONLY:
        raise SandboxViolation(f"Blocked: {target.name} is a frozen acceptance test")
    target.write_text(content)
    return str(target)


def list_files(workdir):
    root = Path(workdir).resolve()
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
