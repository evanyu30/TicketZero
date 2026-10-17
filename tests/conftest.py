"""Offline test setup. Environment must be set before ticketzero is imported (config reads it at import)."""
import os
import tempfile

from fake_nemotron_env import SECRET

os.environ["TICKETZERO_ENV_FILE"] = os.devnull   # never load the developer's real .env (or keys) in tests
os.environ["CRUSOE_API_KEY"] = SECRET
os.environ["YDC_API_KEY"] = ""
os.environ["TICKETZERO_LIVE_TESTS"] = "0"
os.environ["TICKETZERO_HOME"] = tempfile.mkdtemp(prefix="ticketzero_test_")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from fake_nemotron import CATALOG  # noqa: E402
from ticketzero import models  # noqa: E402
from ticketzero.api import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    models.fetch_crusoe_models = lambda: CATALOG   # startup resolves models without the network
    with TestClient(app) as c:
        yield c
