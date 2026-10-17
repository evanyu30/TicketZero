"""Pydantic schemas for every structured LLM output, and lenient JSON extraction."""
import json
import re
from typing import Annotated, Any, Optional

from pydantic import BaseModel, BeforeValidator, ValidationError

from .util import stable_json, trunc


def _to_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        return stable_json(v)
    return str(v)


def _to_str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        v = [v]
    return [_to_str(x) for x in v if x is not None and _to_str(x) != ""]


# Models often return numbers or nested objects where we want strings; coerce instead of failing.
Str = Annotated[str, BeforeValidator(_to_str)]
StrList = Annotated[list[str], BeforeValidator(_to_str_list)]


class TriageOut(BaseModel):
    symptoms: StrList = []
    status_codes: StrList = []
    error_codes: StrList = []
    model_ids: StrList = []
    hostnames: StrList = []
    files: StrList = []
    independent_symptoms: int = 1


class CriterionOut(BaseModel):
    id: Str = ""
    text: Str = ""
    test_ids: StrList = []


class CriteriaOut(BaseModel):
    criteria: list[CriterionOut] = []


class SubtaskOut(BaseModel):
    title: Str
    goal: Str = ""
    hypothesis: Str = ""
    test_ids: StrList = []


class PlanOut(BaseModel):
    kind: Str = "atomic"
    rationale: Str = ""
    approach: Str = ""
    parallel: bool = True
    test_ids: StrList = []
    subtasks: list[SubtaskOut] = []


class EvidenceItem(BaseModel):
    kind: Str = "file"
    ref: Str = ""
    detail: Str = ""


class InvestigationOut(BaseModel):
    root_cause: Str = ""
    evidence: list[EvidenceItem] = []
    confidence: float = 0.0
    proposed_change: Str = ""


class GateOut(BaseModel):
    enough: bool             # required: a missing verdict must not default to "enough"
    missing: Str = ""


class FixOut(BaseModel):
    summary: Str = ""
    diff: Str = ""


class CriticOut(BaseModel):
    veto: bool               # required: a missing verdict must not default to "no veto"
    reason: Str = ""


class NextStepsOut(BaseModel):
    checks: StrList = []


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_model(schema: type[BaseModel], text: str) -> tuple[Optional[BaseModel], str]:
    """Extract the outermost {...} from model output and validate it. Returns (object, error)."""
    if not text or not text.strip():
        return None, "empty content"
    s = text.strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        m = _FENCE_RE.search(s)
        if not m:
            return None, "no JSON object found"
        s = m.group(1)
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            return None, "no JSON object found"
    try:
        data = json.loads(s[i:j + 1])
    except json.JSONDecodeError as e:
        return None, f"JSON decode error: {e}"
    if not isinstance(data, dict):
        return None, "top-level JSON is not an object"
    try:
        return schema.model_validate(data), ""
    except ValidationError as e:
        return None, f"schema validation error: {trunc(str(e), 500)}"


def schema_text(schema: type[BaseModel]) -> str:
    return stable_json(schema.model_json_schema())
