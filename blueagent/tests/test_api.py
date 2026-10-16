"""Tests for FastAPI endpoints and lifespan integration."""

import pytest
from httpx import ASGITransport, AsyncClient
from blueagent.main import app, lifespan
from blueagent.config import settings


@pytest.fixture
async def client(tmp_path, monkeypatch):
    # Configure isolated test database and locks
    test_db = tmp_path / "test_api.db"
    test_locks = tmp_path / "test_api_locks"

    monkeypatch.setattr(settings, "database_path", str(test_db))
    monkeypatch.setattr(settings, "lock_directory", str(test_locks))

    async with lifespan(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac


@pytest.mark.asyncio
async def test_health_endpoints(client):
    # Liveness probe
    res = await client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

    # Readiness probe
    ready_res = await client.get("/health/ready")
    assert ready_res.status_code == 200
    data = ready_res.json()
    assert data["status"] == "ready"
    assert data["database"] == "connected"
    assert data["lifecycle_worker"] == "running"


@pytest.mark.asyncio
async def test_teams_api_templates(client):
    # Without token: 401 Unauthorized
    unauth_res = await client.get("/teams")
    assert unauth_res.status_code == 401

    # With valid Bearer token: 200 OK and empty list
    headers = {"Authorization": f"Bearer {settings.ctfd_api_token}"}
    list_res = await client.get("/teams", headers=headers)
    assert list_res.status_code == 200
    assert list_res.json() == []



@pytest.mark.asyncio
async def test_challenges_api_templates(client):
    headers = {"Authorization": f"Bearer {settings.ctfd_api_token}"}

    # Unauthenticated /teams/1/machines -> 401
    unauth = await client.get("/teams/1/machines")
    assert unauth.status_code == 401

    # Missing team returns 404
    missing_team_res = await client.get("/teams/1/machines", headers=headers)
    assert missing_team_res.status_code == 404

    # Insert team 1 directly in DB to avoid kernel permissions in unit test
    await app.state.db.create_team(team_k=1, name="Team 1", bridge_name="br-team-1", wg_interface="wg-1")

    # Listing empty active containers with token on existing team -> 200
    list_res = await client.get("/teams/1/machines", headers=headers)
    assert list_res.status_code == 200
    assert list_res.json() == []

    # POST /teams/99/verificator returns 404 if team does not exist
    verify_res = await client.post(
        "/teams/99/verificator",
        headers=headers,
        json={
            "challenge_id": "pwn-01",
            "containers": [{"name": "pwn", "image": "pwn:latest", "ip_suffix": 10}],
        },
    )
    assert verify_res.status_code == 404

