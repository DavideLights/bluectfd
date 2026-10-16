"""Integration tests for verification flow, solve recording, and cooldown handling."""

import json
import pytest
import responses

from CTFd.models import Solves, Users, db
from CTFd.plugins.blueagent.client import BlueAgentClient
from CTFd.plugins.blueagent.models import BlueAgentPatchChallenge, BlueAgentTeamMapping
from CTFd.plugins.blueagent.services import verify_and_record_solve

SAMPLE_SPEC = {
    "challenge_id": "test-patch-chal",
    "verifier": {"image": "curl:latest", "ip_suffix": 250},
    "containers": [{"name": "web", "image": "alpine-nginx", "ip_suffix": 10, "limits_memory": "256MB"}],
    "verify_commands": [{"target": "verifier", "cmd": ["curl", "http://{{ip:web}}/"]}],
}


@responses.activate
def test_verify_passed_records_solve(app):
    with app.app_context():
        # Setup test challenge
        chal = BlueAgentPatchChallenge(
            name="Patch Web Service",
            category="Defense",
            value=100,
            spec_raw=json.dumps(SAMPLE_SPEC),
            spec_json=json.dumps(SAMPLE_SPEC),
            verify_cooldown=30,
        )
        db.session.add(chal)
        db.session.commit()

        # Mock BlueAgent /teams/1/verificator returning passed == True
        responses.add(
            responses.POST,
            "http://172.17.0.1:8000/teams/1/verificator",
            json={
                "success": True,
                "passed": True,
                "message": "All exploit audits deflected!",
                "details": {"verification": [{"cmd": ["curl"], "exit_code": 0}]},
            },
            status=200,
        )

        client = BlueAgentClient(base_url="http://172.17.0.1:8000", token="dummy")
        payload, code = verify_and_record_solve(
            challenge=chal,
            team_k=1,
            account_id=1,
            account_type="user",
            user_id=1,
            remote_ip="127.0.0.1",
            client=client,
        )

        assert code == 200
        assert payload["status"] == "passed"
        assert payload["solved"] is True

        # Verify solve record in database
        solve = Solves.query.filter_by(challenge_id=chal.id, user_id=1).first()
        assert solve is not None
        assert solve.provided == "VERIFIED_PATCH"


@responses.activate
def test_verify_cooldown_rejection(app):
    with app.app_context():
        chal = BlueAgentPatchChallenge(
            name="Patch Web Cooldown",
            category="Defense",
            value=100,
            spec_raw=json.dumps(SAMPLE_SPEC),
            spec_json=json.dumps(SAMPLE_SPEC),
            verify_cooldown=60,
        )
        db.session.add(chal)
        db.session.commit()

        # Mock BlueAgent returning passed == False
        responses.add(
            responses.POST,
            "http://172.17.0.1:8000/teams/2/verificator",
            json={"success": True, "passed": False, "message": "Exploit succeeded"},
            status=200,
        )

        client = BlueAgentClient(base_url="http://172.17.0.1:8000", token="dummy")

        # First attempt -> sets cooldown
        p1, c1 = verify_and_record_solve(
            challenge=chal,
            team_k=2,
            account_id=1,
            account_type="user",
            user_id=1,
            remote_ip="127.0.0.1",
            client=client,
        )
        assert c1 == 200
        assert p1["status"] == "failed"

        # Immediate second attempt -> must be blocked by cooldown with 429
        p2, c2 = verify_and_record_solve(
            challenge=chal,
            team_k=2,
            account_id=1,
            account_type="user",
            user_id=1,
            remote_ip="127.0.0.1",
            client=client,
        )
        assert c2 == 429
        assert p2["status"] == "cooldown"
        assert p2["retry_after"] > 0
