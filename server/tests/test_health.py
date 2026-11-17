from fastapi.testclient import TestClient

from enzo import __version__
from enzo.config import Settings
from enzo.gateway.app import create_app


def test_healthz_reports_version_and_env() -> None:
    client = TestClient(create_app(Settings(env="test")))

    resp = client.get("/healthz")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": __version__, "env": "test"}
