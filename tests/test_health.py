from fastapi.testclient import TestClient

from job_radar.api import create_app
from job_radar.config import Settings


def test_health_ok_when_dependencies_are_up(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        r = client.get("/health")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "checks": {"postgres": "ok", "redis": "ok"}}


def test_health_503_names_the_failing_dependency(settings: Settings) -> None:
    broken = settings.model_copy(update={"database_url": "postgresql+asyncpg://x:x@127.0.0.1:1/x"})
    with TestClient(create_app(broken)) as client:
        r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["redis"] == "ok"
    assert body["checks"]["postgres"] != "ok"
    assert "127.0.0.1" not in r.text and "x:x" not in r.text  # no connection details leak to clients
