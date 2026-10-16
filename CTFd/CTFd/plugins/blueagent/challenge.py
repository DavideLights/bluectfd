"""Custom CTFd Challenge Type implementation for BlueAgent Patch Challenges.

Subclasses BaseChallenge to provide:
- IaC challenge definition upload (YAML/JSON)
- Container specification & collision checking
- Player view serialization (sanitizing secret exploit commands)
- Dual-mode solve handling (Automated OVN patch verification & optional static flag fallback)
- Safe challenge deletion with automated container teardown across teams
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from flask import Request

from CTFd.models import Challenges, Flags, db
from CTFd.plugins.challenges import BaseChallenge
from CTFd.plugins.flags import get_flag_class

from .client import BlueAgentClient
from .models import BlueAgentDeployment, BlueAgentPatchChallenge
from .spec import (
    SpecValidationError,
    check_global_collisions,
    parse_spec_text,
    render_spec_for_team,
    validate_spec_structure,
)

logger = logging.getLogger(__name__)


class BlueAgentPatchChallengeType(BaseChallenge):
    """Challenge type for BlueAgent automated patch defense verification."""

    id = "blueagent_patch"
    name = "blueagent_patch"
    challenge_model = BlueAgentPatchChallenge

    templates = {
        "create": "/plugins/blueagent/assets/create.html",
        "update": "/plugins/blueagent/assets/update.html",
        "view": "/plugins/blueagent/assets/view.html",
    }
    scripts = {
        "create": "/plugins/blueagent/assets/create.js",
        "update": "/plugins/blueagent/assets/update.js",
        "view": "/plugins/blueagent/assets/view.js",
    }
    route = "/plugins/blueagent/assets/"

    blueprint = None

    @classmethod
    def create(cls, request: Request) -> BlueAgentPatchChallenge:
        """Parses admin form, validates specification, uploads Distrobuilder image if provided, and saves challenge."""
        data = request.form or request.get_json() or {}

        # 1. Extract specification text from uploaded file or textarea
        spec_text = ""
        if "spec_file" in request.files and request.files["spec_file"].filename:
            file_obj = request.files["spec_file"]
            spec_text = file_obj.read().decode("utf-8")
        elif data.get("spec_text"):
            spec_text = str(data.get("spec_text"))
        else:
            raise SpecValidationError("Please provide a challenge specification (upload file or paste YAML/JSON).")

        spec_dict, spec_fmt = parse_spec_text(spec_text)
        c_names, ip_suffixes = validate_spec_structure(spec_dict)
        check_global_collisions(c_names, ip_suffixes)

        # 2. Handle optional Distrobuilder image upload
        distro_alias = spec_dict.get("containers", [{}])[0].get("image")
        if "distrobuilder_file" in request.files and request.files["distrobuilder_file"].filename:
            db_file = request.files["distrobuilder_file"]
            db_bytes = db_file.read()
            client = BlueAgentClient()
            try:
                img_res = client.build_image(definition_bytes=db_bytes, alias=distro_alias)
                distro_alias = img_res.get("alias", distro_alias)
            except Exception as exc:
                logger.warning("Distrobuilder upload to BlueAgent failed: %s", exc)

        # 3. Create challenge model
        auto_deploy = str(data.get("auto_deploy", "true")).lower() in ("true", "1", "yes")
        show_details = str(data.get("show_verify_details", "false")).lower() in ("true", "1", "yes")
        allow_fallback = str(data.get("allow_flag_fallback", "false")).lower() in ("true", "1", "yes")
        cooldown = int(data.get("verify_cooldown", 30))

        challenge = BlueAgentPatchChallenge(
            name=data.get("name"),
            category=data.get("category"),
            description=data.get("description", ""),
            value=int(data.get("value", 0)),
            state=data.get("state", "hidden"),
            spec_raw=spec_text,
            spec_json=json.dumps(spec_dict),
            spec_format=spec_fmt,
            distro_alias=distro_alias,
            auto_deploy=auto_deploy,
            show_verify_details=show_details,
            verify_cooldown=cooldown,
            allow_flag_fallback=allow_fallback,
            container_names=json.dumps(c_names),
            ip_suffixes=json.dumps(ip_suffixes),
        )

        db.session.add(challenge)
        db.session.commit()

        # If admin entered a static flag in the form, create flag
        flag_val = str(data.get("flag", "")).strip()
        if flag_val:
            flag = Flags(
                challenge_id=challenge.id,
                content=flag_val,
                type="static",
            )
            db.session.add(flag)
            db.session.commit()

        return challenge

    @classmethod
    def read(cls, challenge: BlueAgentPatchChallenge) -> Dict[str, Any]:
        """Serializes challenge details for player/admin presentation.

        CRITICAL: Redacts secret exploit commands and verifier definitions for non-admins!
        """
        data = {
            "id": challenge.id,
            "name": challenge.name,
            "value": challenge.value,
            "description": challenge.description,
            "category": challenge.category,
            "state": challenge.state,
            "max_attempts": challenge.max_attempts,
            "type": challenge.type,
            "type_data": {
                "id": cls.id,
                "name": cls.name,
                "templates": cls.templates,
                "scripts": cls.scripts,
            },
            # BlueAgent specific public metadata
            "auto_deploy": challenge.auto_deploy,
            "verify_cooldown": challenge.verify_cooldown,
            "allow_flag_fallback": challenge.allow_flag_fallback,
            "containers": [
                {
                    "name": c.get("name"),
                    "image": c.get("image"),
                    "ip_suffix": c.get("ip_suffix"),
                    "limits_cpu": c.get("limits_cpu", "1"),
                    "limits_memory": c.get("limits_memory", "256MB"),
                }
                for c in challenge.parsed_spec.get("containers", [])
            ],
        }
        return data

    @classmethod
    def update(cls, challenge: BlueAgentPatchChallenge, request: Request) -> BlueAgentPatchChallenge:
        """Updates challenge properties and re-validates specification if modified."""
        data = request.form or request.get_json() or {}

        for attr in ("name", "category", "description", "value", "state", "max_attempts"):
            if attr in data:
                setattr(challenge, attr, data[attr])

        if "auto_deploy" in data:
            challenge.auto_deploy = str(data["auto_deploy"]).lower() in ("true", "1", "yes")
        if "show_verify_details" in data:
            challenge.show_verify_details = str(data["show_verify_details"]).lower() in ("true", "1", "yes")
        if "allow_flag_fallback" in data:
            challenge.allow_flag_fallback = str(data["allow_flag_fallback"]).lower() in ("true", "1", "yes")
        if "verify_cooldown" in data:
            challenge.verify_cooldown = int(data["verify_cooldown"])

        # Check if specification was updated
        spec_text = None
        if "spec_file" in request.files and request.files["spec_file"].filename:
            spec_text = request.files["spec_file"].read().decode("utf-8")
        elif "spec_text" in data and data["spec_text"].strip():
            spec_text = str(data["spec_text"]).strip()

        if spec_text:
            spec_dict, spec_fmt = parse_spec_text(spec_text)
            c_names, ip_suffixes = validate_spec_structure(spec_dict)
            check_global_collisions(c_names, ip_suffixes, exclude_challenge_id=challenge.id)

            challenge.spec_raw = spec_text
            challenge.spec_json = json.dumps(spec_dict)
            challenge.spec_format = spec_fmt
            challenge.container_names = json.dumps(c_names)
            challenge.ip_suffixes = json.dumps(ip_suffixes)

        db.session.commit()
        return challenge

    @classmethod
    def delete(cls, challenge: BlueAgentPatchChallenge) -> None:
        """Tears down container instances across running teams and removes challenge record."""
        deployments = BlueAgentDeployment.query.filter_by(
            challenge_id=challenge.id,
            status="running",
        ).all()

        client = BlueAgentClient()
        for dep in deployments:
            try:
                rendered = render_spec_for_team(challenge.parsed_spec, dep.team_k)
                client.teardown_machines(team_k=dep.team_k, config=rendered)
            except Exception as exc:
                logger.warning(
                    "Error tearing down challenge %d on team %d during delete: %s",
                    challenge.id,
                    dep.team_k,
                    exc,
                )

        super().delete(challenge)

    @classmethod
    def attempt(cls, challenge: BlueAgentPatchChallenge, request: Request) -> Tuple[bool, str]:
        """Handles challenge submission attempt.

        Supports standard flag comparison only if `allow_flag_fallback` is enabled.
        Otherwise directs players to use the automated 'Verify Patch' action.
        """
        if not challenge.allow_flag_fallback:
            return False, "This is an automated patch defense challenge. Click 'Verify Patch' to test your defense!"

        data = request.form or request.get_json() or {}
        submission = str(data.get("submission", "")).strip()
        flags = Flags.query.filter_by(challenge_id=challenge.id).all()

        for flag in flags:
            try:
                flag_class = get_flag_class(flag.type)
                if flag_class.compare(flag, submission):
                    return True, "Correct flag! Challenge solved."
            except Exception as err:
                logger.error("Error comparing flag for challenge %d: %s", challenge.id, err)

        return False, "Incorrect flag."

    @classmethod
    def solve(cls, user, team, challenge, request) -> None:
        """Records solve in database. Overridden to handle calls without submission form data."""
        data = request.form or request.get_json() or {}
        submission = data.get("submission", "VERIFIED_PATCH")

        from CTFd.models import Solves
        solve = Solves(
            user_id=user.id,
            team_id=team.id if team else None,
            challenge_id=challenge.id,
            ip=request.remote_addr,
            provided=submission,
        )
        db.session.add(solve)
        db.session.commit()

    @classmethod
    def fail(cls, user, team, challenge, request) -> None:
        """Records fail attempt in database."""
        data = request.form or request.get_json() or {}
        submission = data.get("submission", "FAILED_PATCH")

        from CTFd.models import Fails
        fail = Fails(
            user_id=user.id,
            team_id=team.id if team else None,
            challenge_id=challenge.id,
            ip=request.remote_addr,
            provided=submission,
        )
        db.session.add(fail)
        db.session.commit()
