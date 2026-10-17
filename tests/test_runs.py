"""End-to-end runs through the API with a scripted fake Nemotron (live tests disabled)."""
import time

import pytest

from fake_nemotron import SECRET, FakeNemotron
from ticketzero import config


def run_to_end(client, monkeypatch, mode: str, scenario: str, budget: dict | None = None) -> dict:
    fake = FakeNemotron(mode)
    fake.install(monkeypatch)
    run_id = client.post("/runs", json={"scenario": scenario, "budget": budget or {}}).json()["run_id"]
    deadline = time.time() + 120
    while (run := client.get(f"/runs/{run_id}").json())["status"] == "running":
        assert time.time() < deadline, "run did not finish"
        time.sleep(0.2)
    run["tree"] = client.get(f"/runs/{run_id}/tree").json()
    run["report"] = client.get(f"/runs/{run_id}/report").text
    run["patch"] = client.get(f"/runs/{run_id}/patch").text
    run["events"] = client.get(f"/runs/{run_id}/events", params={"limit": 2000}).json()
    run["fake"] = fake
    return run


def nodes(tree: list[dict]) -> list[dict]:
    out = []
    for n in tree:
        out.append(n)
        out.extend(nodes(n["children"]))
    return out


def test_scenario_a_splits_into_parallel_children_and_verifies(client, monkeypatch):
    run = run_to_end(client, monkeypatch, "A", "multi_bug_chat_service")
    assert run["status"] == "verified", run["reason"]
    root = run["tree"][0]
    assert [c["status"] for c in root["children"]] == ["verified"] * 3
    assert {c["depth"] for c in root["children"]} == {1}
    merges = [e for e in run["events"] if e["type"] == "merge"]
    assert len(merges) == 3 and all(e["message"].startswith("merged") for e in merges)
    for fragment in ("api.inference.crusoecloud.com", "nvidia/Nemotron-3-Super", "MAX_TOKENS = 4096"):
        assert fragment in run["patch"]
    assert "tests/" not in run["patch"]
    assert run["usage"]["cache_hit_rate"] == pytest.approx(0.6)


def test_scenario_b_failed_verification_becomes_child(client, monkeypatch):
    run = run_to_end(client, monkeypatch, "B", "tool_loop_ordering")
    assert run["status"] == "verified", run["reason"]
    titles = [n["title"] for n in nodes(run["tree"])]
    assert any(t.startswith("Fix failed verification") for t in titles), titles
    assert "retried" in run["patch"]


def test_scenario_c_escalates_with_bundle(client, monkeypatch):
    run = run_to_end(client, monkeypatch, "C", "voice_audio_unsupported")
    assert run["status"] == "escalated"
    assert (config.HOME / "runs" / run["id"] / "escalation.md").is_file()
    assert "What a human should check next" in run["report"]
    assert not run["patch"]
    assert max(n["depth"] for n in nodes(run["tree"])) <= 4


def test_f1_tiny_budget_escalates_without_hanging(client, monkeypatch):
    run = run_to_end(client, monkeypatch, "budget", "multi_bug_chat_service", {"max_llm_calls": 5})
    assert run["status"] == "escalated"
    assert "budget or depth exceeded" in run["reason"]
    assert run["usage"]["llm_calls"] <= 5


def test_invalid_json_after_repair_fails_node_and_escalates(client, monkeypatch):
    run = run_to_end(client, monkeypatch, "bad_json", "tool_loop_ordering")
    assert run["status"] == "escalated"
    assert run["tree"][0]["status"] == "failed"
    assert "invalid JSON after one repair call" in run["reason"]


def test_secret_never_reaches_prompts_or_database(client, monkeypatch):
    run = run_to_end(client, monkeypatch, "A", "multi_bug_chat_service")
    assert not any(SECRET in p for p in run["fake"].prompts_seen)
    db_bytes = b"".join(p.read_bytes() for p in config.HOME.glob("ticketzero.db*"))
    assert SECRET.encode() not in db_bytes


def test_health_and_scenarios(client, monkeypatch):
    FakeNemotron("A").install(monkeypatch)
    health = client.get("/health").json()
    assert health["ok"] and health["crusoe_key_present"] and not health["ydc_key_present"]
    assert health["models"]["verifier"] != health["models"]["fixer"]
    names = [s["name"] for s in client.get("/scenarios").json()]
    assert names[:3] == ["multi_bug_chat_service", "tool_loop_ordering", "voice_audio_unsupported"]
    tickets = [n[:3] for n in names[3:]]                                       # benchmark tickets, in order
    assert tickets == sorted(tickets) and {f"T{i:02d}" for i in range(1, 11)} <= set(tickets)


def test_split_children_are_told_about_sibling_tasks(client, monkeypatch):
    run = run_to_end(client, monkeypatch, "A", "multi_bug_chat_service")
    acting_roles = ("You are the Planner", "You are a leaf Investigator", "You are the Fixer")
    host_prompts = [p for p in run["fake"].prompts_seen
                    if "Title: Fix API host" in p and any(r in p for r in acting_roles)]
    assert host_prompts, "no prompt for the Fix API host child"
    for p in host_prompts:
        assert "do NOT fix these" in p and "Fix model ID" in p and "Fix token budget" in p
        assert "- Fix API host:" not in p   # a child is never listed as its own sibling
