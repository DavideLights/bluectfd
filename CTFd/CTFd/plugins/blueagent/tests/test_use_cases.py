"""End-to-End Use Case Tests for BlueAgent CTFd Plugin.

Validates complete real-world scenarios across:
1. Team Management & WireGuard VPN (provisioning, member config, telemetry, reset, and purge)
2. Container Management & Images (catalog, build, compilation logs, bulk deploy, teardown, and delete)
3. Challenge Creation & Verification (validation, creation, redaction, start env, verify passed/failed, and audit log)
"""

import io
import json
import pytest
import responses

from CTFd.models import Challenges, Fails, Flags, Solves, Teams, Users, db
from CTFd.plugins.blueagent.challenge import BlueAgentPatchChallengeType
from CTFd.plugins.blueagent.client import BlueAgentClient
from CTFd.plugins.blueagent.locks import refund_cooldown
from CTFd.plugins.blueagent.models import (
    BlueAgentDeployment,
    BlueAgentPatchChallenge,
    BlueAgentTeamMapping,
    BlueAgentVerifyLog,
)
from CTFd.plugins.blueagent.services import (
    deploy_challenge_for_team,
    ensure_member_vpn,
    ensure_team_provisioned,
    teardown_challenge_for_team,
    verify_and_record_solve,
)
from CTFd.utils import set_config
from tests.helpers import login_as_user, register_user

SAMPLE_CHALLENGE_SPEC = """
challenge_id: "sec-patch-webapp"
verifier:
  image: "curlimages/curl:latest"
  ip_suffix: 250
  limits_cpu: "1"
  limits_memory: "256MB"
containers:
  - name: "app"
    image: "alpine-nginx-sec"
    ip_suffix: 10
    limits_cpu: "1"
    limits_memory: "512MB"
readiness_commands:
  - target: "container"
    container_name: "app"
    cmd: ["pgrep", "nginx"]
    timeout: 5
    interval: 1
    retry_timeout: 10
verify_commands:
  - target: "verifier"
    cmd: ["curl", "-sf", "http://{{ip:app}}:80/"]
    timeout: 10
"""


def _reset_db(app):
    """Helper to ensure clean state across test cases."""
    with app.app_context():
        BlueAgentVerifyLog.query.delete()
        BlueAgentDeployment.query.delete()
        BlueAgentTeamMapping.query.delete()
        Solves.query.delete()
        Fails.query.delete()
        Challenges.query.delete()
        set_config("user_mode", "teams")
        db.session.commit()


def test_use_case_team_management(app):
    """Scenario 1: Team lifecycle, provisioning, member VPN generation, telemetry, reset, and purge."""
    _reset_db(app)

    with app.app_context():
        team_name = "AlphaTeam"
        register_user(app, name="player_alpha", email="alpha@test.com", password="password")
        player_user = Users.query.filter_by(name="player_alpha").first()
        ctfd_team = Teams(name=team_name, email="alpha-team@test.com", captain_id=player_user.id)
        db.session.add(ctfd_team)
        db.session.commit()
        player_user.team_id = ctfd_team.id
        db.session.commit()

        team_id = ctfd_team.id
        player_id = player_user.id

        # 1. Deterministic team_k allocation
        mapping = BlueAgentTeamMapping.get_or_create(account_id=team_id, account_type="team")
        team_k = mapping.team_k
        assert 1 <= team_k <= 254

    # 2. Admin provisions team infrastructure via API
    admin_client = login_as_user(app, name="admin", password="password")
    with admin_client.session_transaction() as sess:
        admin_nonce = sess.get("nonce")

    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        # PUT /teams/{team_k}
        rsps.add(
            responses.PUT,
            f"http://127.0.0.1:8000/teams/{team_k}",
            json={"team_k": team_k, "name": team_name, "bridge": f"br-team{team_k}"},
            status=201,
        )
        rsps.add(
            responses.GET,
            f"http://127.0.0.1:8000/teams/{team_k}",
            json={"team_k": team_k, "name": team_name},
            status=200,
        )
        rsps.add(
            responses.GET,
            "http://127.0.0.1:8000/health/ready",
            json={"status": "ready", "version": "2.0"},
            status=200,
        )

        res_prov = admin_client.post(
            f"/admin/plugins/blueagent/api/teams/{team_id}/provision",
            json={"nonce": admin_nonce},
        )
        assert res_prov.status_code == 200
        assert res_prov.get_json()["success"] is True

        with app.app_context():
            # Mapping updated to provisioned in DB
            m_updated = BlueAgentTeamMapping.query.filter_by(team_k=team_k).first()
            assert m_updated.provisioned is True

        # 3. Player downloads WireGuard VPN configuration
        player_client = login_as_user(app, name="player_alpha", password="password")

        # PUT /teams/{team_k}/members/{user_id}
        wg_conf_mock = (
            f"[Interface]\nPrivateKey = privkey123\nAddress = 10.{team_k}.0.2/24\n\n"
            f"[Peer]\nPublicKey = srvpubkey\nEndpoint = 198.51.100.1:51820\nAllowedIPs = 10.{team_k}.0.0/16\n"
        )
        rsps.add(
            responses.PUT,
            f"http://127.0.0.1:8000/teams/{team_k}/members/{player_id}",
            json={
                "member_id": f"ctfd-{team_k}-{player_id}",
                "team_k": team_k,
                "name": "player_alpha",
                "client_ip": f"10.{team_k}.0.2/24",
                "public_key": "userpubkey",
                "config_text": wg_conf_mock,
            },
            status=201,
        )

        res_vpn = player_client.get("/api/v1/plugins/blueagent/vpn/download")
        assert res_vpn.status_code == 200
        assert res_vpn.mimetype == "text/plain"
        vpn_text = res_vpn.get_data(as_text=True)
        assert f"Address = 10.{team_k}.0.2/24" in vpn_text
        assert "Content-Disposition" in res_vpn.headers

        # 4. Player checks live VPN status telemetry
        rsps.add(
            responses.GET,
            f"http://127.0.0.1:8000/teams/{team_k}/members",
            json=[
                {
                    "member_id": f"ctfd-{team_k}-{player_id}",
                    "team_k": team_k,
                    "name": "player_alpha",
                    "user_id": player_id,
                    "client_ip": f"10.{team_k}.0.2/24",
                    "telemetry": {
                        "online": True,
                        "endpoint": "203.0.113.10:51820",
                        "latest_handshake": 1700000000,
                        "transfer_rx": 4096,
                        "transfer_tx": 8192,
                    },
                }
            ],
            status=200,
        )
        res_status = player_client.get("/api/v1/plugins/blueagent/vpn/status")
        assert res_status.status_code == 200
        status_data = res_status.get_json()
        assert status_data["connected"] is True
        assert len(status_data["peers"]) == 1

        # 5. Admin checks team WireGuard telemetry via Admin API
        res_peers = admin_client.get(f"/admin/plugins/blueagent/api/teams/{team_k}/peers")
        assert res_peers.status_code == 200
        peers_data = res_peers.get_json()
        assert peers_data["success"] is True
        assert peers_data["peers"][0]["online"] is True

        # 6. Admin resets WireGuard peers
        rsps.add(
            responses.DELETE,
            f"http://127.0.0.1:8000/teams/{team_k}/members/{player_id}",
            status=204,
        )
        res_reset = admin_client.post(
            f"/admin/plugins/blueagent/api/teams/{team_k}/reset-wg",
            json={"nonce": admin_nonce},
        )
        assert res_reset.status_code == 200
        assert res_reset.get_json()["success"] is True

        # 7. Admin purges team environment completely
        rsps.add(
            responses.DELETE,
            f"http://127.0.0.1:8000/teams/{team_k}",
            status=204,
        )
        res_purge = admin_client.delete(
            f"/admin/plugins/blueagent/api/teams/{team_k}",
            json={"nonce": admin_nonce},
        )
        assert res_purge.status_code == 200
        assert res_purge.get_json()["success"] is True

        # Mapping and deployments deleted
        with app.app_context():
            assert BlueAgentTeamMapping.query.filter_by(team_k=team_k).first() is None


def test_use_case_container_management(app):
    """Scenario 2: Distrobuilder image catalog, upload, compilation logs, machine monitoring, bulk deploy, and teardown."""
    _reset_db(app)

    admin_client = login_as_user(app, name="admin", password="password")
    with admin_client.session_transaction() as sess:
        admin_nonce = sess.get("nonce")

    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        # 1. List images in catalog
        rsps.add(
            responses.GET,
            "http://127.0.0.1:8000/images",
            json=[{"alias": "alpine-3.20-nginx", "status": "ready", "fingerprint": "a1b2c3d4e5f6"}],
            status=200,
        )
        res_list = admin_client.get("/admin/plugins/blueagent/api/images")
        assert res_list.status_code == 200
        assert len(res_list.get_json()["images"]) == 1

        # 2. Upload / Build Distrobuilder YAML definition
        rsps.add(
            responses.PUT,
            "http://127.0.0.1:8000/images/alpine-nginx-sec",
            json={"alias": "alpine-nginx-sec", "status": "building", "fingerprint": None},
            status=202,
        )
        yaml_bytes = b"image:\n  description: alpine-nginx-sec\n"
        upload_data = {
            "alias": "alpine-nginx-sec",
            "file": (io.BytesIO(yaml_bytes), "definition.yaml"),
            "force": "true",
            "nonce": admin_nonce,
        }
        res_upload = admin_client.post(
            "/admin/plugins/blueagent/api/images",
            data=upload_data,
            content_type="multipart/form-data",
        )
        assert res_upload.status_code == 200
        assert res_upload.get_json()["image"]["alias"] == "alpine-nginx-sec"

        # 3. View real-time build compilation logs
        rsps.add(
            responses.GET,
            "http://127.0.0.1:8000/images/alpine-nginx-sec/logs",
            body="[1/4] Unpacking base rootfs\n[2/4] Running package installs\n[OK] Build completed.\n",
            content_type="text/plain",
            status=200,
        )
        res_logs = admin_client.get("/admin/plugins/blueagent/api/images/alpine-nginx-sec/logs")
        assert res_logs.status_code == 200
        assert "[OK] Build completed." in res_logs.get_data(as_text=True)

        # 4. View active machines across teams
        with app.app_context():
            # Setup team mapping
            mapping = BlueAgentTeamMapping.get_or_create(account_id=201, account_type="team")
            mapping.provisioned = True
            db.session.commit()
            team_k = mapping.team_k

        rsps.add(
            responses.GET,
            f"http://127.0.0.1:8000/teams/{team_k}/machines",
            json=[
                {
                    "instance_name": f"team{team_k}-app",
                    "spec_name": "app",
                    "ip_address": f"10.{team_k}.1.10",
                    "image_alias": "alpine-nginx-sec",
                    "status": "RUNNING",
                }
            ],
            status=200,
        )
        res_mach = admin_client.get("/admin/plugins/blueagent/api/machines")
        assert res_mach.status_code == 200
        mach_data = res_mach.get_json()
        assert len(mach_data["machines"]) >= 1
        assert mach_data["machines"][0]["spec_name"] == "app"

        # 5. Bulk Deploy Challenge to provisioned teams
        with app.app_context():
            chal = BlueAgentPatchChallenge(
                name="Bulk Deploy Chal",
                category="Defense",
                value=200,
                spec_raw=SAMPLE_CHALLENGE_SPEC,
                spec_json=json.dumps({"containers": [{"name": "app", "image": "alpine-nginx-sec", "ip_suffix": 10}]}),
            )
            db.session.add(chal)
            db.session.commit()
            chal_id = chal.id

        rsps.add(
            responses.POST,
            f"http://127.0.0.1:8000/teams/{team_k}/machines",
            json={"containers": [{"name": f"team{team_k}-app", "ip": f"10.{team_k}.1.10"}]},
            status=201,
        )
        res_deploy = admin_client.post(
            f"/admin/plugins/blueagent/api/challenges/{chal_id}/deploy",
            json={"team_ks": [team_k], "nonce": admin_nonce},
        )
        assert res_deploy.status_code == 200
        deploy_res = res_deploy.get_json()
        assert deploy_res["results"][0]["success"] is True

        # 6. Teardown challenge containers for team
        rsps.add(
            responses.DELETE,
            f"http://127.0.0.1:8000/teams/{team_k}/machines",
            status=204,
        )
        res_teardown = admin_client.post(
            f"/admin/plugins/blueagent/api/teams/{team_k}/challenges/{chal_id}/teardown",
            json={"nonce": admin_nonce},
        )
        assert res_teardown.status_code == 200
        assert res_teardown.get_json()["success"] is True

        # 7. Delete Image safeguard check (blocked if challenge references it)
        with app.app_context():
            chal_ref = BlueAgentPatchChallenge.query.filter_by(id=chal_id).first()
            chal_ref.spec_json = json.dumps({"containers": [{"name": "app", "image": "alpine-nginx-sec"}]})
            db.session.commit()

        res_del_blocked = admin_client.delete(
            "/admin/plugins/blueagent/api/images/alpine-nginx-sec",
            json={"nonce": admin_nonce},
        )
        assert res_del_blocked.status_code == 400
        assert "currently referenced" in res_del_blocked.get_json()["message"]

        # 8. Delete Image after reference removed
        with app.app_context():
            db.session.delete(chal_ref)
            db.session.commit()

        rsps.add(
            responses.DELETE,
            "http://127.0.0.1:8000/images/alpine-nginx-sec",
            status=204,
        )
        res_del_ok = admin_client.delete(
            "/admin/plugins/blueagent/api/images/alpine-nginx-sec",
            json={"nonce": admin_nonce},
        )
        assert res_del_ok.status_code == 200
        assert res_del_ok.get_json()["success"] is True


def test_use_case_challenge_creation_and_solve_flow(app):
    """Scenario 3: Challenge creation, redaction, start environment, dark OVN verification, and solve award."""
    _reset_db(app)

    admin_client = login_as_user(app, name="admin", password="password")
    with admin_client.session_transaction() as sess:
        admin_nonce = sess.get("nonce")

    # 1. Validate spec via Client-Side helper API
    res_val = admin_client.post(
        "/admin/plugins/blueagent/api/spec/validate",
        json={"spec_text": SAMPLE_CHALLENGE_SPEC, "nonce": admin_nonce},
    )
    assert res_val.status_code == 200
    val_data = res_val.get_json()
    assert val_data["success"] is True
    assert val_data["containers"][0]["name"] == "app"
    assert val_data["verify_count"] == 1

    # 2. Collision detection rejection on duplicate container names
    bad_spec = """
challenge_id: "duplicate-test"
verifier:
  image: "curl"
  ip_suffix: 250
containers:
  - name: "app"
    image: "alpine"
    ip_suffix: 10
  - name: "app"
    image: "alpine"
    ip_suffix: 11
verify_commands:
  - target: "verifier"
    cmd: ["true"]
"""
    res_bad = admin_client.post(
        "/admin/plugins/blueagent/api/spec/validate",
        json={"spec_text": bad_spec, "nonce": admin_nonce},
    )
    assert res_bad.status_code == 400
    assert "Duplicate container name 'app'" in res_bad.get_json()["message"]

    # 3. Create Challenge in CTFd
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses.PUT,
            "http://127.0.0.1:8000/images/alpine-nginx-sec",
            json={"alias": "alpine-nginx-sec", "status": "ready"},
            status=201,
        )

        with app.app_context():
            # Setup player user and team
            register_user(app, name="defense_player", email="def@test.com", password="password")
            user = Users.query.filter_by(name="defense_player").first()
            team = Teams(name="DefenseTeam", email="defteam@test.com", captain_id=user.id)
            db.session.add(team)
            db.session.commit()
            user.team_id = team.id
            db.session.commit()

            user_id = user.id
            mapping = BlueAgentTeamMapping.get_or_create(account_id=team.id, account_type="team")
            mapping.provisioned = True
            db.session.commit()
            team_k = mapping.team_k

            chal = BlueAgentPatchChallenge(
                name="Cyber Patch Challenge 1",
                category="Defense",
                description="Patch the Nginx app to prevent unauthorized access.",
                value=250,
                state="visible",
                spec_raw=SAMPLE_CHALLENGE_SPEC,
                spec_json=json.dumps({
                    "challenge_id": "sec-patch-webapp",
                    "verifier": {"image": "curlimages/curl:latest", "ip_suffix": 250},
                    "containers": [{"name": "app", "image": "alpine-nginx-sec", "ip_suffix": 10}],
                    "verify_commands": [{"target": "verifier", "cmd": ["curl", "-sf", f"http://10.{team_k}.1.10:80/"]}],
                }),
                distro_alias="alpine-nginx-sec",
                auto_deploy=True,
                show_verify_details=True,
                verify_cooldown=30,
            )
            db.session.add(chal)
            db.session.commit()
            chal_id = chal.id

        # 4. Challenge Read Serialization Redaction Check
        with app.app_context():
            chal_obj = BlueAgentPatchChallenge.query.get(chal_id)
            serialized = BlueAgentPatchChallengeType.read(chal_obj)
            # Verifier definitions and secret exploit commands MUST be redacted
            assert "verify_commands" not in serialized
            assert "verifier" not in serialized
            # Containers public info should be present
            assert serialized["containers"][0]["name"] == "app"
            assert serialized["containers"][0]["ip_suffix"] == 10

        player_client = login_as_user(app, name="defense_player", password="password")
        with player_client.session_transaction() as sess:
            player_nonce = sess.get("nonce")

        # 5. Player checks challenge status
        res_player_status = player_client.get(f"/api/v1/plugins/blueagent/status?challenge_id={chal_id}")
        assert res_player_status.status_code == 200
        p_status = res_player_status.get_json()
        assert p_status["success"] is True
        assert p_status["subnet"] == f"10.{team_k}.1.0/24"
        assert p_status["targets"][0]["ip"] == f"10.{team_k}.1.10"

        # 6. Player starts challenge environment
        rsps.add(
            responses.PUT,
            f"http://127.0.0.1:8000/teams/{team_k}",
            json={"team_k": team_k, "name": "DefenseTeam", "bridge": f"br-team{team_k}"},
            status=200,
        )
        rsps.add(
            responses.POST,
            f"http://127.0.0.1:8000/teams/{team_k}/machines",
            json={"containers": [{"name": f"team{team_k}-app", "ip": f"10.{team_k}.1.10"}]},
            status=201,
        )
        res_start = player_client.post(
            f"/api/v1/plugins/blueagent/challenges/{chal_id}/start",
            json={"nonce": player_nonce},
        )
        assert res_start.status_code == 200
        assert res_start.get_json()["status"] == "running"

        # 6b. Player tears down challenge environment (containers only, preserving team network)
        rsps.add(
            responses.DELETE,
            f"http://127.0.0.1:8000/teams/{team_k}/machines",
            status=204,
        )
        res_teardown = player_client.post(
            f"/api/v1/plugins/blueagent/challenges/{chal_id}/teardown",
            json={"nonce": player_nonce},
        )
        assert res_teardown.status_code == 200
        assert res_teardown.get_json()["status"] == "torn_down"

        # Re-start challenge environment for subsequent verification tests
        res_restart = player_client.post(
            f"/api/v1/plugins/blueagent/challenges/{chal_id}/start",
            json={"nonce": player_nonce},
        )
        assert res_restart.status_code == 200
        assert res_restart.get_json()["status"] == "running"

        # 7. Verification Failure Flow
        rsps.add(
            responses.POST,
            f"http://127.0.0.1:8000/teams/{team_k}/verificator",
            json={
                "success": True,
                "passed": False,
                "message": "Exploit probe bypassed defense: HTTP 200 returned on vulnerable endpoint.",
                "details": {"probe": "GET /vulnerable", "exit_code": 1},
            },
            status=200,
        )
        res_verify_fail = player_client.post(
            f"/api/v1/plugins/blueagent/challenges/{chal_id}/verify",
            json={"nonce": player_nonce},
        )
        assert res_verify_fail.status_code == 200
        fail_data = res_verify_fail.get_json()
        assert fail_data["success"] is False
        assert fail_data["status"] == "failed"
        assert "bypassed defense" in fail_data["message"]

        # Check Fail record in DB
        with app.app_context():
            fail_record = Fails.query.filter_by(challenge_id=chal_id, user_id=user_id).first()
            assert fail_record is not None

        # 8. Cooldown Enforcement: Immediate retry returns 429
        res_verify_cooldown = player_client.post(
            f"/api/v1/plugins/blueagent/challenges/{chal_id}/verify",
            json={"nonce": player_nonce},
        )
        assert res_verify_cooldown.status_code == 429
        assert res_verify_cooldown.get_json()["status"] == "cooldown"

        # 9. Verification Passed Flow (after refunding cooldown)
        refund_cooldown(team_k, chal_id)

        rsps.replace(
            responses.POST,
            f"http://127.0.0.1:8000/teams/{team_k}/verificator",
            json={
                "success": True,
                "passed": True,
                "message": "All exploit audits deflected! Services healthy.",
                "details": {"checks": 4, "passed": 4},
            },
            status=200,
        )
        res_verify_pass = player_client.post(
            f"/api/v1/plugins/blueagent/challenges/{chal_id}/verify",
            json={"nonce": player_nonce},
        )
        assert res_verify_pass.status_code == 200
        pass_data = res_verify_pass.get_json()
        assert pass_data["success"] is True
        assert pass_data["status"] == "passed"
        assert pass_data["solved"] is True

        # Check Solve record in CTFd DB
        with app.app_context():
            solve_record = Solves.query.filter_by(challenge_id=chal_id, user_id=user_id).first()
            assert solve_record is not None
            assert solve_record.provided == "VERIFIED_PATCH"

        # 10. Repeat verification on already-solved challenge returns "already_solved"
        res_repeat = player_client.post(
            f"/api/v1/plugins/blueagent/challenges/{chal_id}/verify",
            json={"nonce": player_nonce},
        )
        assert res_repeat.status_code == 200
        assert res_repeat.get_json()["status"] == "already_solved"

        # 11. Admin views Verification Audit Log
        res_audit = admin_client.get("/admin/plugins/blueagent/api/verify-log?limit=10")
        assert res_audit.status_code == 200
        audit_data = res_audit.get_json()
        assert audit_data["success"] is True
        assert len(audit_data["logs"]) >= 2
        # Check passed and failed logs exist
        passed_logs = [l for l in audit_data["logs"] if l["passed"] is True]
        assert len(passed_logs) >= 1


def test_use_case_ui_templates_and_fallback_flags(app):
    """Scenario 4: Validate UI template rendering, DOM elements, and fallback flag logic."""
    _reset_db(app)

    # 1. Admin Dashboard View rendering
    admin_client = login_as_user(app, name="admin", password="password")
    res_dash = admin_client.get("/admin/plugins/blueagent")
    assert res_dash.status_code == 200
    html = res_dash.get_data(as_text=True)

    # Verify key dashboard DOM sections & tabs
    assert "BlueAgent Control Center" in html
    assert "tab-teams" in html
    assert "tab-containers" in html
    assert "tab-images" in html
    assert "tab-logs" in html
    assert "tab-settings" in html

    # Verify table and modal containers
    assert 'id="table-teams-overview"' in html
    assert 'id="table-machines-overview"' in html
    assert 'id="table-images-overview"' in html
    assert 'id="table-verify-logs"' in html
    assert 'id="modal-peer-telemetry"' in html
    assert 'id="modal-deploy-challenge"' in html
    assert 'id="modal-build-image"' in html
    assert 'id="modal-image-logs"' in html
    assert "admin.js" in html

    # 2. Flag fallback behavior tests
    with app.app_context():
        # Setup player
        register_user(app, name="flag_player", email="fp@test.com", password="password")
        player_user = Users.query.filter_by(name="flag_player").first()

        # Challenge A: Fallback Flag DISABLED (pure patch defense)
        chal_no_fallback = BlueAgentPatchChallenge(
            name="No Fallback Chal",
            category="Defense",
            value=100,
            spec_raw=SAMPLE_CHALLENGE_SPEC,
            spec_json=json.dumps({"containers": []}),
            allow_flag_fallback=False,
        )
        db.session.add(chal_no_fallback)
        db.session.commit()

        # Dummy request object mock
        class FakeRequest:
            form = {"submission": "flag{some_secret}"}

        success, msg = BlueAgentPatchChallengeType.attempt(chal_no_fallback, FakeRequest())
        assert success is False
        assert "Click 'Verify Patch'" in msg

        # Challenge B: Fallback Flag ENABLED
        chal_fallback = BlueAgentPatchChallenge(
            name="Fallback Chal",
            category="Defense",
            value=100,
            spec_raw=SAMPLE_CHALLENGE_SPEC,
            spec_json=json.dumps({"containers": []}),
            allow_flag_fallback=True,
        )
        db.session.add(chal_fallback)
        db.session.commit()

        flag = Flags(
            challenge_id=chal_fallback.id,
            content="flag{backup_secret_pass}",
            type="static",
        )
        db.session.add(flag)
        db.session.commit()

        # Incorrect flag submission
        class WrongRequest:
            form = {"submission": "flag{wrong_guess}"}

        success_wrong, msg_wrong = BlueAgentPatchChallengeType.attempt(chal_fallback, WrongRequest())
        assert success_wrong is False
        assert "Incorrect flag" in msg_wrong

        # Correct flag submission
        class CorrectRequest:
            form = {"submission": "flag{backup_secret_pass}"}

        success_ok, msg_ok = BlueAgentPatchChallengeType.attempt(chal_fallback, CorrectRequest())
        assert success_ok is True
        assert "Correct flag" in msg_ok

