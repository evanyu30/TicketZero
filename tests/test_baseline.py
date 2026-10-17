"""#4 and T08: a failure that does not reproduce escalates without a patch; tests are listed even when the app
crashes on import."""
import time

from fake_nemotron import FakeNemotron
from ticketzero import prompts
from ticketzero.verify import static_test_ids


def wait(client, run_id):
    deadline = time.time() + 120
    while (run := client.get(f"/runs/{run_id}").json())["status"] == "running":
        assert time.time() < deadline
        time.sleep(0.2)
    return run


def test_platform_incident_escalates_without_patching(client, monkeypatch, tmp_path):
    """Like T07/T10: the code is fine and its tests pass; the logs show a past outage."""
    (tmp_path / "app.py").write_text("def answer():\n    return 42\n")
    (tmp_path / "test_app.py").write_text("from app import answer\n\n\ndef test_answer():\n    assert answer() == 42\n")
    fake = FakeNemotron("A")
    fake.install(monkeypatch)
    run = wait(client, client.post("/runs", json={"folder": str(tmp_path), "ticket": {
        "description": "Everything failed last night between 02:10 and 02:40 UTC. Is it our code?",
        "logs": "02:11:04Z ERROR POST /v1/chat/completions -> 503 Service Unavailable"}}).json()["run_id"])
    assert run["status"] == "escalated"
    assert run["reason"].startswith("No code bug to fix")
    assert not client.get(f"/runs/{run['id']}/patch").text
    fixer_calls = [p for p in fake.prompts_seen if "You are the Fixer" in p]
    assert fixer_calls == []                                    # nothing was patched
    events = client.get(f"/runs/{run['id']}/events", params={"limit": 2000}).json()
    assert any(e["type"] == "plan" and e["message"].startswith("baseline: all") for e in events)
    assert "What a human should check next" in client.get(f"/runs/{run['id']}/report").text


def test_failing_baseline_still_goes_through_the_fix_flow(client, monkeypatch):
    FakeNemotron("A").install(monkeypatch)
    run = wait(client, client.post("/runs", json={"scenario": "multi_bug_chat_service"}).json()["run_id"])
    events = client.get(f"/runs/{run['id']}/events", params={"limit": 2000}).json()
    assert any(e["message"].startswith("baseline: the failure reproduces") for e in events)
    assert run["status"] == "verified"


def test_static_test_ids_when_the_app_crashes_on_import(tmp_path):
    """Like T08: app.py raises at import, so pytest collects nothing; the IDs come from the test file."""
    (tmp_path / "test_app.py").write_text(
        "from app import ask\n\n\ndef test_ask():\n    pass\n\n\nasync def test_async():\n    pass\n\n\n"
        "class TestCard:\n    def test_card(self):\n        pass\n\n    def helper(self):\n        pass\n\n\n"
        "def helper():\n    pass\n")
    (tmp_path / "conftest.py").write_text("def test_not_a_test():\n    pass\n")
    assert static_test_ids(tmp_path, ["conftest.py", "test_app.py"]) == [
        "test_app.py::test_ask", "test_app.py::test_async", "test_app.py::TestCard::test_card"]


def test_t08_style_folder_gets_criteria(client, monkeypatch, tmp_path):
    (tmp_path / "app.py").write_text("import os\nKEY = os.environ['OPENAI_API_KEY']\n\n\ndef ask():\n    return 'ok'\n")
    (tmp_path / "test_app.py").write_text("from app import ask\n\n\ndef test_ask():\n    assert ask() == 'ok'\n")
    FakeNemotron("A").install(monkeypatch)
    run = wait(client, client.post("/runs", json={"folder": str(tmp_path), "ticket": {
        "description": "Auth failing after migration from OpenAI; CRUSOE_API_KEY is set."}}).json()["run_id"])
    assert any("test_app.py::test_ask" in c["test_ids"] for c in run["criteria"])
