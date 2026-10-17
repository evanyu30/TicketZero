"""Task input and node result types passed through the recursion."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TaskSpec:
    title: str
    goal: str
    hypothesis: str = ""
    test_ids: list[str] = field(default_factory=list)   # empty means the full frozen suite
    note: str = ""                                       # replan notes and failure context

    def text(self) -> str:
        s = self.goal
        if self.hypothesis:
            s += f"\nHypothesis: {self.hypothesis}"
        if self.note:
            s += f"\nNotes: {self.note}"
        return s


@dataclass
class NodeResult:
    node_id: str
    status: str                  # verified | escalated | failed | cancelled
    reason: str = ""
    patch: str = ""
    proof: Optional[dict] = None
    findings: str = ""           # returned to the parent ("Return findings to parent")
    title: str = ""
