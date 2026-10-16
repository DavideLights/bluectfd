"""Route integration tests for player status and admin APIs."""

import json
import pytest

from CTFd.models import Teams, Users, db
from CTFd.plugins.blueagent.models import BlueAgentPatchChallenge, BlueAgentTeamMapping
from tests.helpers import login_as_user, register_user


def test_player_status_unauthenticated(client):
    res = client.get("/api/v1/plugins/blueagent/status")
    # Unauthenticated redirects to login in CTFd
    assert res.status_code in (302, 403)


def test_admin_overview_forbidden_for_regular_user(app, client):
    with app.app_context():
        # Register normal user
        register_user(app, name="player1", email="player1@test.com", password="password")

    client = login_as_user(app, name="player1", password="password")
    res = client.get("/admin/plugins/blueagent/api/overview")
    assert res.status_code in (302, 403)


def test_admin_spec_validation_api(app):
    client = login_as_user(app, name="admin", password="password")

    valid_spec = """
challenge_id: "test-admin-validate"
verifier:
  image: "curlimages/curl"
  ip_suffix: 250
containers:
  - name: "srv"
    image: "alpine"
    ip_suffix: 15
    limits_memory: "256MB"
verify_commands:
  - target: "verifier"
    cmd: ["curl", "http://{{ip:srv}}/"]
"""

    res = client.post(
        "/admin/plugins/blueagent/api/spec/validate",
        json={"spec_text": valid_spec},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["containers"][0]["name"] == "srv"


def test_admin_images_and_logs_api(app):
    import io
    import responses

    admin_client = login_as_user(app, name="admin", password="password")

    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        # 1. Test GET /admin/plugins/blueagent/api/images
        rsps.add(
            responses.GET,
            "http://127.0.0.1:8000/images",
            json=[{"alias": "alpine-base", "status": "ready"}],
            status=200,
        )
        res = admin_client.get("/admin/plugins/blueagent/api/images")
        assert res.status_code == 200
        assert res.get_json()["images"][0]["alias"] == "alpine-base"

        # 2. Test GET /admin/plugins/blueagent/api/images/<alias>/logs
        rsps.add(
            responses.GET,
            "http://127.0.0.1:8000/images/alpine-base/logs",
            body="Image build complete successfully.",
            content_type="text/plain",
            status=200,
        )
        res = admin_client.get("/admin/plugins/blueagent/api/images/alpine-base/logs")
        assert res.status_code == 200
        assert "complete successfully" in res.get_data(as_text=True)

        # 3. Test POST /admin/plugins/blueagent/api/images
        rsps.add(
            responses.PUT,
            "http://127.0.0.1:8000/images/custom-alias",
            json={"alias": "custom-alias", "status": "building"},
            status=202,
        )
        with admin_client.session_transaction() as sess:
            nonce = sess.get("nonce")

        upload_data = {
            "alias": "custom-alias",
            "file": (io.BytesIO(b"image:\n  description: test\n"), "definition.yaml"),
            "nonce": nonce,
        }
        res = admin_client.post(
            "/admin/plugins/blueagent/api/images",
            data=upload_data,
            headers={"CSRF-Token": nonce},
            content_type="multipart/form-data",
        )
        assert res.status_code == 200
        assert res.get_json()["image"]["alias"] == "custom-alias"

        # 4. Test POST /admin/plugins/blueagent/api/images with yaml_text (pasted definition)
        rsps.add(
            responses.PUT,
            "http://127.0.0.1:8000/images/pasted-alias",
            json={"alias": "pasted-alias", "status": "building"},
            status=202,
        )
        res_paste = admin_client.post(
            "/admin/plugins/blueagent/api/images",
            data={
                "alias": "pasted-alias",
                "yaml_text": "image:\n  distribution: ubuntu\n  release: jammy\n",
                "nonce": nonce,
            },
            headers={"CSRF-Token": nonce},
            content_type="multipart/form-data",
        )
        assert res_paste.status_code == 200
        assert res_paste.get_json()["image"]["alias"] == "pasted-alias"


