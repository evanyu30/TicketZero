"""Working folders: what gets copied, starting a run from a folder, and applying a verified result back."""
import shutil
import time

import pytest

from fake_nemotron import FakeNemotron
from ticketzero.scenarios import SCENARIOS
from ticketzero.workspace import FolderError, require_tests, scan_folder

DEMO_A = SCENARIOS["multi_bug_chat_service"]["folder"]


def test_scan_skips_secrets_vendored_dirs_and_finds_tests(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("x = 1\n")
    (tmp_path / "test_main.py").write_text("def test_x(): pass\n")
    (tmp_path / ".env").write_text("CRUSOE_API_KEY=secret\n")
    (tmp_path / ".env.example").write_text("CRUSOE_API_KEY=\n")
    (tmp_path / "server.pem").write_text("-----BEGIN-----\n")
    for d in ("node_modules/pkg", ".venv/lib", "__pycache__"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "f.py").write_text("junk\n")
    scan = scan_folder(str(tmp_path))
    assert set(scan.files) == {"app/main.py", "test_main.py", ".env.example"}
    assert set(scan.skipped_secrets) == {".env", "server.pem"}
    assert scan.test_files == ["test_main.py"]


def test_scan_rejects_missing_folder_and_folder_without_tests(tmp_path):
    with pytest.raises(FolderError, match="not found"):
        scan_folder(str(tmp_path / "nope"))
    (tmp_path / "main.py").write_text("x = 1\n")
    with pytest.raises(FolderError, match="No pytest tests"):
        require_tests(scan_folder(str(tmp_path)))


def test_folder_check_endpoint(client):
    summary = client.get("/fs/check", params={"path": str(DEMO_A)}).json()
    assert summary["test_file_count"] == 2 and summary["files"] >= 6
    assert client.get("/fs/check", params={"path": "/definitely/not/here"}).status_code == 400


def wait(client, run_id):
    deadline = time.time() + 120
    while (run := client.get(f"/runs/{run_id}").json())["status"] == "running":
        assert time.time() < deadline
        time.sleep(0.2)
    return run


def test_run_on_user_folder_then_apply_back(client, monkeypatch, tmp_path):
    folder = tmp_path / "my-service"
    shutil.copytree(DEMO_A, folder, ignore=shutil.ignore_patterns("__pycache__"))
    original = (folder / "app/config.py").read_text()
    FakeNemotron("A").install(monkeypatch)
    res = client.post("/runs", json={"folder": str(folder), "ticket": {
        "description": "Our chat endpoint started failing\nSome requests 404.", "logs": "404 model_not_found"}})
    run = wait(client, res.json()["run_id"])
    assert run["status"] == "verified" and run["source_dir"] == str(folder) and not run["read_only"]
    assert run["ticket"]["title"] == "Our chat endpoint started failing"
    assert (folder / "app/config.py").read_text() == original          # the run itself never writes the folder

    applied = client.post(f"/runs/{run['id']}/apply").json()
    assert applied["files"] == ["app/config.py"]
    new = (folder / "app/config.py").read_text()
    assert "api.inference.crusoecloud.com" in new and "MAX_TOKENS = 4096" in new
    assert client.post(f"/runs/{run['id']}/apply").status_code == 409  # never twice


def test_apply_refuses_when_folder_changed_and_for_demos(client, monkeypatch, tmp_path):
    folder = tmp_path / "svc"
    shutil.copytree(DEMO_A, folder, ignore=shutil.ignore_patterns("__pycache__"))
    FakeNemotron("A").install(monkeypatch)
    run = wait(client, client.post("/runs", json={"folder": str(folder),
                                                  "ticket": {"description": "broken"}}).json()["run_id"])
    (folder / "app/config.py").write_text("# edited by the user meanwhile\n")
    r = client.post(f"/runs/{run['id']}/apply")
    assert r.status_code == 409 and "changed in your folder" in r.json()["detail"]
    assert (folder / "app/config.py").read_text() == "# edited by the user meanwhile\n"

    demo = wait(client, client.post("/runs", json={"scenario": "multi_bug_chat_service"}).json()["run_id"])
    assert demo["read_only"] and client.post(f"/runs/{demo['id']}/apply").status_code == 409


def test_custom_run_requires_description(client, monkeypatch):
    FakeNemotron("A").install(monkeypatch)
    r = client.post("/runs", json={"folder": str(DEMO_A), "ticket": {"description": "  "}})
    assert r.status_code == 400


def test_file_browsing_endpoints_hide_secrets(client, tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / ".env").write_text("CRUSOE_API_KEY=secret\n")
    listing = client.get("/fs/files", params={"path": str(tmp_path)}).json()
    assert [e["path"] for e in listing["entries"]] == ["app.py"] and listing["name"] == tmp_path.name
    assert client.get("/fs/file", params={"path": str(tmp_path), "file": "app.py"}).json()["text"] == "x = 1\n"
    assert client.get("/fs/file", params={"path": str(tmp_path), "file": ".env"}).status_code == 404
    assert client.get("/fs/file", params={"path": str(tmp_path), "file": "../etc/passwd"}).status_code == 400


def test_scenarios_carry_example_labels_and_tickets(client):
    first = client.get("/scenarios").json()[0]
    assert first["label"] and first["hint"] and first["ticket"]["description"] and first["folder"].endswith("/repo")


def test_cors_allows_only_local_origins(client):
    ok = client.get("/health", headers={"Origin": "http://127.0.0.1:5173"})
    assert ok.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"
    assert client.get("/health", headers={"Origin": "null"}).headers.get("access-control-allow-origin") == "null"
    assert "access-control-allow-origin" not in client.get("/health", headers={"Origin": "https://evil.example"}).headers


def test_answer_key_is_never_visible_to_agents(tmp_path):
    (tmp_path / "tickets/T01").mkdir(parents=True)
    (tmp_path / "tickets/T01/app.py").write_text("x = 1\n")
    (tmp_path / "tickets/T01/test_app.py").write_text("def test_x(): pass\n")
    (tmp_path / "_answer_key").mkdir()
    (tmp_path / "_answer_key/answers.json").write_text('{"root_cause": "secret"}')
    assert not any("_answer_key" in p for p in scan_folder(str(tmp_path)).files)
