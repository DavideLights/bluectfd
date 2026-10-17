import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport
from blueagent.models import ChallengeConfig, ContainerSpec, CommandSpec, VerifierSpec
from blueagent.main import app


def test_template_rendering_placeholders():
    cfg = ChallengeConfig(
        challenge_id="web-ch",
        verifier=VerifierSpec(ip_suffix=250),
        containers=[
            ContainerSpec(name="web", image="alpine", ip_suffix=10),
            ContainerSpec(name="db", image="alpine", ip_suffix=20),
        ],
        readiness_commands=[
            CommandSpec(
                target="container",
                container_name="web",
                cmd=["curl", "-s", "http://{{ip:db}}:5432", "--header", "X-Team: {{team_k}}"],
                env={"SUBNET": "{{subnet}}", "VERIFIER": "{{verifier_ip}}"},
            )
        ],
        verify_commands=[
            CommandSpec(
                target="verifier",
                cmd=["curl", "-f", "http://{{ip:web}}:8080/flag"],
                env={"TARGET": "{{ip:web}}", "SUBNET": "{{subnet}}"},
            )
        ],
    )

    rendered = cfg.render_for_team(7)
    assert rendered.team_k == 7

    rcmd = rendered.readiness_commands[0]
    assert rcmd.cmd == ["curl", "-s", "http://10.7.1.20:5432", "--header", "X-Team: 7"]
    assert rcmd.env["SUBNET"] == "10.7.1"
    assert rcmd.env["VERIFIER"] == "10.7.1.250"

    vcmd = rendered.verify_commands[0]
    assert vcmd.cmd == ["curl", "-f", "http://10.7.1.10:8080/flag"]
    assert vcmd.env["TARGET"] == "10.7.1.10"
    assert vcmd.env["SUBNET"] == "10.7.1"


@pytest.mark.asyncio
async def test_delete_machines_with_query_param():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("blueagent.api.challenges.ChallengeService") as mock_service_cls:
            mock_svc = AsyncMock()
            app.dependency_overrides = {}
            from blueagent.dependencies import get_challenge_service, require_ctfd_token
            app.dependency_overrides[get_challenge_service] = lambda: mock_svc
            app.dependency_overrides[require_ctfd_token] = lambda: True

            try:
                # 1. Query param ?containers=web,db
                res = await client.delete("/teams/3/machines?containers=web,db")
                assert res.status_code == 204
                mock_svc.teardown_challenge.assert_called_with(
                    team_k=3,
                    challenge_id="ch",
                    container_names=["web", "db"],
                )

                # 2. No query param & no body -> tears down all containers
                res2 = await client.delete("/teams/3/machines")
                assert res2.status_code == 204
                mock_svc.teardown_challenge.assert_called_with(
                    team_k=3,
                    challenge_id="ch",
                    container_names=None,
                )
            finally:
                app.dependency_overrides.clear()
