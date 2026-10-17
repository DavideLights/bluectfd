"""Flask routes and API endpoints for BlueAgent CTFd plugin.

Exposes:
- Player challenge APIs (VPN configuration download, deployment trigger, live verify probe)
- Admin dashboard view (/admin/plugins/blueagent)
- Admin management APIs (team provision/teardown, peer telemetry, bulk deploy, image catalog)
"""

import json
import logging
from typing import Any, Dict, List

from flask import (
    Blueprint,
    Response,
    abort,
    jsonify,
    render_template,
    request,
)

from CTFd.models import Challenges, Solves, Teams, Users, db
from CTFd.utils import get_config, set_config
from CTFd.utils.decorators import admins_only, authed_only
from CTFd.utils.user import get_current_user

from .client import BlueAgentClient, BlueAgentError, BlueAgentUnavailable
from .config import settings
from .models import (
    BlueAgentDeployment,
    BlueAgentPatchChallenge,
    BlueAgentTeamMapping,
    BlueAgentVerifyLog,
    CapacityError,
)
from .services import (
    deploy_challenge_for_team,
    ensure_member_vpn,
    ensure_team_provisioned,
    get_current_account_context,
    get_team_mapping_for_account,
    teardown_challenge_for_team,
    verify_and_record_solve,
)
from .spec import (
    SpecValidationError,
    check_global_collisions,
    parse_spec_text,
    validate_spec_structure,
)

logger = logging.getLogger(__name__)

blueagent_bp = Blueprint(
    "blueagent",
    __name__,
    template_folder="templates",
    static_folder="assets",
    url_prefix="",
)


# =========================================================================
# Player Routes
# =========================================================================


@blueagent_bp.route("/api/v1/plugins/blueagent/status", methods=["GET"])
@authed_only
def get_player_status():
    """Retrieves current team's network addressing, deployment state, and targets."""
    try:
        account_id, account_type, account_name, user_id = get_current_account_context()
    except PermissionError as err:
        return jsonify({"success": False, "message": str(err)}), 403

    try:
        mapping = get_team_mapping_for_account(account_id, account_type)
        team_k = mapping.team_k
    except CapacityError as cap_err:
        return jsonify({"success": False, "message": str(cap_err)}), 507

    chal_id_str = request.args.get("challenge_id")
    dep_status = "idle"
    targets: List[Dict[str, Any]] = []
    solved = False

    if chal_id_str:
        try:
            chal_id = int(chal_id_str)
            challenge = BlueAgentPatchChallenge.query.filter_by(id=chal_id).first()
            if challenge:
                deployment = BlueAgentDeployment.query.filter_by(
                    team_k=team_k,
                    challenge_id=chal_id,
                ).first()
                if deployment:
                    dep_status = deployment.status

                # Build target IP table
                for c in challenge.parsed_spec.get("containers", []):
                    targets.append({
                        "name": c.get("name"),
                        "ip": f"10.{team_k}.1.{c.get('ip_suffix')}",
                        "limits_cpu": c.get("limits_cpu", "1"),
                        "limits_memory": c.get("limits_memory", "256MB"),
                    })

                # Check solve state
                team_val = account_id if account_type == "team" else None
                s = Solves.query.filter_by(
                    challenge_id=chal_id,
                    team_id=team_val,
                ).first() if team_val else Solves.query.filter_by(
                    challenge_id=chal_id,
                    user_id=user_id,
                ).first()
                solved = s is not None
        except ValueError:
            pass

    return jsonify({
        "success": True,
        "team_k": team_k,
        "account_name": account_name,
        "subnet": f"10.{team_k}.1.0/24",
        "wg_client_subnet": f"10.{team_k}.0.0/24",
        "deployment_status": dep_status,
        "solved": solved,
        "targets": targets,
    })


@blueagent_bp.route("/api/v1/plugins/blueagent/challenges/<int:challenge_id>/start", methods=["POST"])
@authed_only
def start_challenge_environment(challenge_id: int):
    """Deploys containers for challenge on team bridge."""
    try:
        account_id, account_type, account_name, user_id = get_current_account_context()
    except PermissionError as err:
        return jsonify({"success": False, "message": str(err)}), 403

    challenge = BlueAgentPatchChallenge.query.filter_by(id=challenge_id).first_or_404()
    mapping = get_team_mapping_for_account(account_id, account_type)
    team_k = mapping.team_k

    ensure_team_provisioned(team_k, account_name)

    try:
        res = deploy_challenge_for_team(team_k, challenge)
        return jsonify({"success": True, "status": res.get("status"), "message": "Environment started successfully."})
    except BlueAgentUnavailable as unavail:
        return jsonify({"success": False, "message": f"Orchestrator unavailable: {unavail}"}), 503
    except Exception as exc:
        logger.exception("Error starting challenge environment: %s", exc)
        return jsonify({"success": False, "message": f"Deployment failed: {exc}"}), 500


@blueagent_bp.route("/api/v1/plugins/blueagent/challenges/<int:challenge_id>/teardown", methods=["POST"])
@authed_only
def teardown_challenge_environment(challenge_id: int):
    """Tears down containers for challenge on team bridge while preserving team network interfaces."""
    try:
        account_id, account_type, account_name, user_id = get_current_account_context()
    except PermissionError as err:
        return jsonify({"success": False, "message": str(err)}), 403

    challenge = BlueAgentPatchChallenge.query.filter_by(id=challenge_id).first_or_404()
    mapping = get_team_mapping_for_account(account_id, account_type)
    team_k = mapping.team_k

    try:
        teardown_challenge_for_team(team_k, challenge)
        return jsonify({
            "success": True,
            "status": "torn_down",
            "message": "Challenge environment torn down successfully.",
        })
    except BlueAgentUnavailable as unavail:
        return jsonify({"success": False, "message": f"Orchestrator unavailable: {unavail}"}), 503
    except Exception as exc:
        logger.exception("Error tearing down challenge environment: %s", exc)
        return jsonify({"success": False, "message": f"Teardown failed: {exc}"}), 500


@blueagent_bp.route("/api/v1/plugins/blueagent/vpn/download", methods=["GET"])
@authed_only
def download_vpn_config():
    """Generates WireGuard configuration text and returns as downloadable file attachment."""
    user = get_current_user()
    try:
        account_id, account_type, account_name, user_id = get_current_account_context()
    except PermissionError as err:
        return jsonify({"success": False, "message": str(err)}), 403

    mapping = get_team_mapping_for_account(account_id, account_type)
    team_k = mapping.team_k

    try:
        member = ensure_member_vpn(
            team_k=team_k,
            user_id=user.id,
            username=user.name,
            team_name=account_name,
        )
    except Exception as exc:
        logger.error("Error generating VPN member for user %d: %s", user.id, exc)
        return jsonify({"success": False, "message": f"VPN profile creation failed: {exc}"}), 500

    config_text = member.get("config_text", "")
    if not config_text:
        return jsonify({"success": False, "message": "Failed to generate WireGuard configuration."}), 500

    filename = f"team-{team_k}-{user.name}.conf".replace(" ", "_")
    return Response(
        config_text,
        mimetype="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )


@blueagent_bp.route("/api/v1/plugins/blueagent/vpn/status", methods=["GET"])
@authed_only
def get_vpn_telemetry():
    """Returns live peer status for player's team."""
    try:
        account_id, account_type, account_name, user_id = get_current_account_context()
    except PermissionError as err:
        return jsonify({"success": False, "message": str(err)}), 403

    mapping = get_team_mapping_for_account(account_id, account_type)
    team_k = mapping.team_k

    client = BlueAgentClient()
    try:
        peers = client.get_peers(team_k)
        # Find entry for current user if ctfd_user_id is available
        user_peer = next((p for p in peers if p.get("ctfd_user_id") == user_id), None)
        return jsonify({
            "success": True,
            "connected": user_peer.get("online", False) if user_peer else False,
            "peers": peers,
        })
    except Exception:
        return jsonify({"success": True, "connected": False, "peers": []})


@blueagent_bp.route("/api/v1/plugins/blueagent/challenges/<int:challenge_id>/verify", methods=["POST"])
@authed_only
def verify_patch_endpoint(challenge_id: int):
    """Executes live verification against dark OVN sandbox and awards solve on pass."""
    try:
        account_id, account_type, account_name, user_id = get_current_account_context()
    except PermissionError as err:
        return jsonify({"success": False, "message": str(err)}), 403

    challenge = BlueAgentPatchChallenge.query.filter_by(id=challenge_id).first_or_404()
    mapping = get_team_mapping_for_account(account_id, account_type)
    team_k = mapping.team_k
    remote_ip = request.remote_addr or "127.0.0.1"

    result_payload, status_code = verify_and_record_solve(
        challenge=challenge,
        team_k=team_k,
        account_id=account_id,
        account_type=account_type,
        user_id=user_id,
        remote_ip=remote_ip,
    )
    result_payload["success"] = result_payload.get("status") in ("passed", "already_solved")
    return jsonify(result_payload), status_code


# =========================================================================
# Admin Routes
# =========================================================================


@blueagent_bp.route("/admin/plugins/blueagent", methods=["GET"])
@admins_only
def admin_dashboard_view():
    """Renders main BlueAgent administration control center."""
    return render_template("blueagent/admin_dashboard.html")


@blueagent_bp.route("/admin/plugins/blueagent/api/overview", methods=["GET"])
@admins_only
def admin_overview_api():
    """Returns overview statistics: BlueAgent health, teams mapping, active containers."""
    client = BlueAgentClient()
    health_status = {"online": False, "version": "unknown"}
    try:
        h = client.health_ready()
        health_status = {"online": True, "version": h.get("version", "2.0"), "details": h}
    except Exception as exc:
        health_status = {"online": False, "error": str(exc)}

    # Fetch CTFd accounts and mappings
    mode = get_config("user_mode") or "teams"
    mappings = BlueAgentTeamMapping.query.order_by(BlueAgentTeamMapping.team_k).all()
    mapping_dict = {m.account_id: m for m in mappings}

    account_list = []
    if mode == "teams":
        ctfd_teams = Teams.query.all()
        for t in ctfd_teams:
            m = mapping_dict.get(t.id)
            account_list.append({
                "account_id": t.id,
                "name": t.name,
                "type": "team",
                "team_k": m.team_k if m else None,
                "provisioned": m.provisioned if m else False,
            })
    else:
        ctfd_users = Users.query.all()
        for u in ctfd_users:
            m = mapping_dict.get(u.id)
            account_list.append({
                "account_id": u.id,
                "name": u.name,
                "type": "user",
                "team_k": m.team_k if m else None,
                "provisioned": m.provisioned if m else False,
            })

    # Total challenges
    challenges = BlueAgentPatchChallenge.query.all()
    deployments = BlueAgentDeployment.query.all()

    return jsonify({
        "success": True,
        "mode": mode,
        "health": health_status,
        "accounts": account_list,
        "challenges": [{"id": c.id, "name": c.name, "distro_alias": c.distro_alias} for c in challenges],
        "deployments_count": len(deployments),
        "settings": {
            "blueagent_url": settings.url,
            "verify_cooldown": settings.default_verify_cooldown,
        },
    })


@blueagent_bp.route("/admin/plugins/blueagent/api/teams/<int:account_id>/provision", methods=["POST"])
@admins_only
def admin_provision_team(account_id: int):
    """Manually provisions bridge and WireGuard tunnel for an account."""
    mode = get_config("user_mode") or "teams"
    name = f"Team-{account_id}"
    if mode == "teams":
        t = Teams.query.filter_by(id=account_id).first_or_404()
        name = t.name
    else:
        u = Users.query.filter_by(id=account_id).first_or_404()
        name = u.name

    mapping = get_team_mapping_for_account(account_id, mode.rstrip("s"))
    try:
        res = ensure_team_provisioned(mapping.team_k, name)
        return jsonify({"success": True, "result": res})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@blueagent_bp.route("/admin/plugins/blueagent/api/teams/<int:team_k>/peers", methods=["GET"])
@admins_only
def admin_get_team_peers(team_k: int):
    """Fetches real-time WireGuard peer telemetry for team_k."""
    client = BlueAgentClient()
    try:
        peers = client.get_peers(team_k)
        return jsonify({"success": True, "team_k": team_k, "peers": peers})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@blueagent_bp.route("/admin/plugins/blueagent/api/teams/<int:team_k>/reset-wg", methods=["POST"])
@admins_only
def admin_reset_wireguard(team_k: int):
    """Removes all member peers for team_k in WireGuard (requiring members to re-download configs)."""
    client = BlueAgentClient()
    try:
        members = client.list_members(team_k)
        for m in members:
            client.delete_member(team_k, m.get("user_id") or m.get("member_id"))
        return jsonify({"success": True, "message": f"Reset WireGuard peers for team {team_k}."})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@blueagent_bp.route("/admin/plugins/blueagent/api/teams/<int:team_k>", methods=["DELETE"])
@admins_only
def admin_delete_team_environment(team_k: int):
    """Purges all containers, bridges, WireGuard tunnels, and records for team_k."""
    client = BlueAgentClient()
    try:
        client.delete_team(team_k)
    except Exception as exc:
        logger.warning("Error deleting team on BlueAgent: %s", exc)

    # Delete plugin deployment and mapping records
    BlueAgentDeployment.query.filter_by(team_k=team_k).delete()
    BlueAgentTeamMapping.query.filter_by(team_k=team_k).delete()
    db.session.commit()

    return jsonify({"success": True, "message": f"Team {team_k} infrastructure and mapping purged."})


@blueagent_bp.route("/admin/plugins/blueagent/api/machines", methods=["GET"])
@admins_only
def admin_list_machines():
    """Aggregates all running container instances across teams."""
    client = BlueAgentClient()
    mappings = BlueAgentTeamMapping.query.filter_by(provisioned=True).all()
    all_machines = []

    for m in mappings:
        try:
            machines = client.get_active_machines(m.team_k)
            for mach in machines:
                mach["team_k"] = m.team_k
                all_machines.append(mach)
        except Exception:
            pass

    return jsonify({"success": True, "machines": all_machines})


@blueagent_bp.route("/admin/plugins/blueagent/api/challenges/<int:challenge_id>/deploy", methods=["POST"])
@admins_only
def admin_bulk_deploy_challenge(challenge_id: int):
    """Deploys challenge to a list of teams or all provisioned teams."""
    challenge = BlueAgentPatchChallenge.query.filter_by(id=challenge_id).first_or_404()
    data = request.get_json() or {}
    target_ks = data.get("team_ks", "all")

    if target_ks == "all":
        mappings = BlueAgentTeamMapping.query.filter_by(provisioned=True).all()
        team_ks = [m.team_k for m in mappings]
    elif isinstance(target_ks, list):
        team_ks = [int(k) for k in target_ks]
    else:
        team_ks = []

    results = []
    for k in team_ks:
        try:
            res = deploy_challenge_for_team(k, challenge)
            results.append({"team_k": k, "success": True, "status": res.get("status")})
        except Exception as exc:
            results.append({"team_k": k, "success": False, "error": str(exc)})

    return jsonify({"success": True, "results": results})


@blueagent_bp.route(
    "/admin/plugins/blueagent/api/teams/<int:team_k>/challenges/<int:challenge_id>/teardown",
    methods=["POST"],
)
@admins_only
def admin_teardown_team_challenge(team_k: int, challenge_id: int):
    """Tears down challenge container topology for a specific team."""
    challenge = BlueAgentPatchChallenge.query.filter_by(id=challenge_id).first_or_404()
    try:
        teardown_challenge_for_team(team_k, challenge)
        return jsonify({"success": True, "message": f"Challenge {challenge_id} torn down for team {team_k}."})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@blueagent_bp.route("/admin/plugins/blueagent/api/images", methods=["GET", "POST"])
@admins_only
def admin_images_api():
    """Lists images or uploads a new Distrobuilder definition."""
    client = BlueAgentClient()

    if request.method == "GET":
        try:
            images = client.list_images()
            return jsonify({"success": True, "images": images})
        except Exception as exc:
            return jsonify({"success": False, "message": str(exc)}), 500

    # POST - Upload Distrobuilder YAML
    alias = request.form.get("alias") or (request.is_json and request.get_json().get("alias"))
    if alias and str(alias).strip():
        alias = str(alias).strip().lower()
    else:
        alias = None

    force = str(request.form.get("force", "false")).lower() in ("true", "1", "yes")
    if request.is_json and request.get_json().get("force"):
        force = bool(request.get_json().get("force"))

    file_obj = request.files.get("file") or request.files.get("definition")
    if file_obj and file_obj.filename:
        definition_bytes = file_obj.read()
    else:
        def_text = request.form.get("yaml_text") or request.form.get("definition")
        if not def_text and request.is_json:
            def_text = request.get_json().get("yaml_text") or request.get_json().get("definition")
        if def_text and str(def_text).strip():
            definition_bytes = str(def_text).strip().encode("utf-8")
        elif file_obj:
            definition_bytes = file_obj.read()
        else:
            return jsonify({"success": False, "message": "Missing Distrobuilder YAML file or definition text."}), 400

    try:
        res = client.build_image(definition_bytes=definition_bytes, alias=alias, force=force)
        return jsonify({"success": True, "image": res})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@blueagent_bp.route("/admin/plugins/blueagent/api/images/<string:alias>/logs", methods=["GET"])
@admins_only
def admin_image_logs(alias: str):
    """Retrieves real-time or completed build compilation logs."""
    client = BlueAgentClient()
    try:
        logs_text = client.get_image_logs(alias)
        return Response(logs_text, mimetype="text/plain")
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@blueagent_bp.route("/admin/plugins/blueagent/api/images/<string:alias>", methods=["DELETE"])
@admins_only
def admin_delete_image(alias: str):
    """Deletes an image from the catalog."""
    # Check if any challenge references this image
    challenges = BlueAgentPatchChallenge.query.all()
    for c in challenges:
        for cont in c.parsed_spec.get("containers", []):
            if cont.get("image") == alias:
                return jsonify({
                    "success": False,
                    "message": f"Image '{alias}' is currently referenced by challenge '{c.name}' (ID {c.id}).",
                }), 400

    client = BlueAgentClient()
    try:
        client.delete_image(alias)
        return jsonify({"success": True, "message": f"Image '{alias}' deleted."})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@blueagent_bp.route("/admin/plugins/blueagent/api/verify-log", methods=["GET"])
@admins_only
def admin_get_verify_log():
    """Returns recent verification audit logs."""
    limit = min(int(request.args.get("limit", 100)), 500)
    logs = (
        BlueAgentVerifyLog.query.order_by(BlueAgentVerifyLog.id.desc())
        .limit(limit)
        .all()
    )
    return jsonify({
        "success": True,
        "logs": [
            {
                "id": l.id,
                "team_k": l.team_k,
                "challenge_id": l.challenge_id,
                "user_id": l.user_id,
                "passed": l.passed,
                "success": l.success,
                "message": l.message,
                "duration_ms": l.duration_ms,
                "created_at": l.created_at.isoformat() if l.created_at else "",
            }
            for l in logs
        ],
    })


@blueagent_bp.route("/admin/plugins/blueagent/api/settings", methods=["GET", "POST"])
@admins_only
def admin_settings_api():
    """Reads or updates dynamic plugin settings in CTFd database."""
    if request.method == "GET":
        return jsonify({
            "success": True,
            "blueagent_url": settings.url,
            "verify_cooldown": settings.default_verify_cooldown,
        })

    data = request.get_json() or {}
    if "blueagent_url" in data:
        set_config("blueagent_url", str(data["blueagent_url"]).strip())
    if "blueagent_token" in data and str(data["blueagent_token"]).strip():
        set_config("blueagent_token", str(data["blueagent_token"]).strip())
    if "verify_cooldown" in data:
        set_config("blueagent_verify_cooldown", str(data["verify_cooldown"]).strip())

    return jsonify({"success": True, "message": "Settings updated successfully."})


@blueagent_bp.route("/admin/plugins/blueagent/api/spec/validate", methods=["POST"])
@admins_only
def admin_validate_spec():
    """Client-side preview helper validating specification content before submission."""
    data = request.get_json() or {}
    spec_text = data.get("spec_text", "")
    exclude_id = data.get("exclude_challenge_id")

    try:
        spec_dict, spec_fmt = parse_spec_text(spec_text)
        c_names, ip_suffixes = validate_spec_structure(spec_dict)
        check_global_collisions(c_names, ip_suffixes, exclude_challenge_id=exclude_id)
        return jsonify({
            "success": True,
            "format": spec_fmt,
            "containers": spec_dict.get("containers", []),
            "verifier": spec_dict.get("verifier", {}),
            "readiness_count": len(spec_dict.get("readiness_commands", [])),
            "verify_count": len(spec_dict.get("verify_commands", [])),
        })
    except SpecValidationError as err:
        return jsonify({"success": False, "message": str(err)}), 400
