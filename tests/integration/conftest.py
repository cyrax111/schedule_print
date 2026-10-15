import os
from urllib.parse import urlsplit

import httpx
import pytest

SIM_URL = os.environ.get("U1SIM_URL", "http://localhost:7125")
ALLOWED_HOSTS = {"localhost", "127.0.0.1"}


@pytest.fixture(scope="session")
def sim_url() -> str:
    host = urlsplit(SIM_URL).hostname
    if host not in ALLOWED_HOSTS:
        pytest.exit(f"refusing to run integration tests against {host}: "
                    "only the local simulator is allowed", returncode=2)
    try:
        httpx.get(f"{SIM_URL}/server/info", timeout=3).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("u1sim is not running; start it with scripts/sim-up.sh")
    return SIM_URL
