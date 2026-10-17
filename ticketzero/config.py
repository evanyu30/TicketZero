"""Settings read from the environment (and the project's .env file), plus fixed endpoints and limits."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.environ.get("TICKETZERO_ENV_FILE", PROJECT_ROOT / ".env"))


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ. Variables already set in the environment win.

    Supports comments, blank lines, an optional `export ` prefix, and quoted values. Values are never
    interpolated, so keys containing `$` are safe with or without quotes.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key.isidentifier():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()   # inline comment on an unquoted value
        os.environ.setdefault(key, value)


load_env_file(ENV_FILE)

CRUSOE_BASE_URL = "https://api.inference.crusoecloud.com/v1"
YDC_SEARCH_URL = "https://ydc-index.io/v1/search"
YDC_CONTENTS_URL = "https://ydc-index.io/v1/contents"

CRUSOE_API_KEY = os.environ.get("CRUSOE_API_KEY", "")
YDC_API_KEY = os.environ.get("YDC_API_KEY", "")
HOME = Path(os.environ.get("TICKETZERO_HOME", "./ticketzero_data")).resolve()
RPM_PER_MODEL = int(os.environ.get("TICKETZERO_RPM_PER_MODEL", "25"))
# Crusoe also caps concurrent requests per model (429 "max_requests_limit"), separately from RPM.
MAX_INFLIGHT_PER_MODEL = max(1, int(os.environ.get("TICKETZERO_MAX_INFLIGHT_PER_MODEL", "2")))
LIVE_TESTS = os.environ.get("TICKETZERO_LIVE_TESTS", "1").strip() != "0"

PARENT_RESERVE_CALLS = 4  # calls a parent keeps for its own aggregation and re-verification
FROZEN_CONFIG_NAMES = ("conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg")
PYTEST_TIMEOUT_SECONDS = 180
