import pytest

from . import health


@pytest.fixture(autouse=True)
def fresh_readiness_cache():
    health.reset_readiness_cache()
    yield
    health.reset_readiness_cache()


def test_live_returns_ok_without_auth_or_database(client):
    # No django_db mark on purpose: liveness must never touch the database.
    response = client.get("/api/health/live/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_live_is_never_cached(client):
    response = client.get("/api/health/live/")
    assert "no-store" in response["Cache-Control"]


def test_live_accepts_head_and_rejects_post(client):
    assert client.head("/api/health/live/").status_code == 200
    assert client.post("/api/health/live/").status_code == 405


@pytest.mark.django_db
def test_ready_ok_when_dependencies_are_up(client, monkeypatch):
    monkeypatch.setattr(health, "check_redis", lambda: True)
    response = client.get("/api/health/ready/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


def test_ready_503_when_redis_is_down(client, monkeypatch):
    monkeypatch.setattr(health, "check_database", lambda: True)
    monkeypatch.setattr(health, "check_redis", lambda: False)
    response = client.get("/api/health/ready/")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "checks": {"database": "ok", "redis": "fail"}}


def test_ready_503_when_database_is_down(client, monkeypatch):
    monkeypatch.setattr(health, "check_database", lambda: False)
    monkeypatch.setattr(health, "check_redis", lambda: True)
    response = client.get("/api/health/ready/")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "fail"


def test_ready_never_leaks_error_text(client, monkeypatch):
    class BrokenRedis:
        def ping(self):
            raise ConnectionError("secret-host.internal:6379 refused the connection")

    monkeypatch.setattr(health, "check_database", lambda: True)
    monkeypatch.setattr(health, "get_redis", lambda: BrokenRedis())
    response = client.get("/api/health/ready/")
    assert response.status_code == 503
    assert b"secret-host" not in response.content
    assert b"refused" not in response.content


def test_ready_results_are_memoized_then_expire(client, monkeypatch):
    calls = {"db": 0}

    def fake_db():
        calls["db"] += 1
        return True

    monkeypatch.setattr(health, "check_database", fake_db)
    monkeypatch.setattr(health, "check_redis", lambda: True)

    client.get("/api/health/ready/")
    client.get("/api/health/ready/")
    assert calls["db"] == 1  # second request served from the 2-second memo

    monkeypatch.setattr(health, "READINESS_TTL_SECONDS", 0)
    client.get("/api/health/ready/")
    assert calls["db"] == 2
