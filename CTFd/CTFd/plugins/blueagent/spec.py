"""Specification parser, validator, collision detector, and template renderer for BlueAgent challenges.

Handles:
- Parsing YAML and JSON specifications
- Structural validation matching BlueAgent v2 ChallengeConfig contract
- Cross-challenge container name & IP suffix uniqueness enforcement
- Deterministic per-team template interpolation ({{team_k}}, {{subnet}}, {{ip:<name>}}, {{verifier_ip}})
"""

import copy
import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

# Regex patterns
RE_CHALLENGE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,24}$")
RE_CONTAINER_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,20}$")
RE_LIMITS_MEM = re.compile(r"^\d+(MB|GB)$", re.IGNORECASE)
RE_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_\-:]+)\s*\}\}")


class SpecValidationError(Exception):
    """Raised when challenge specification is invalid or violates constraints."""


def parse_spec_text(raw_text: str) -> Tuple[Dict[str, Any], str]:
    """Parses YAML or JSON text into Python dictionary, returning (spec_dict, format_name)."""
    if not raw_text or not raw_text.strip():
        raise SpecValidationError("Challenge specification content cannot be empty.")

    if len(raw_text) > 256 * 1024:
        raise SpecValidationError("Specification exceeds maximum size limit of 256KB.")

    raw_stripped = raw_text.strip()
    fmt = "json" if raw_stripped.startswith("{") else "yaml"

    try:
        if fmt == "json":
            data = json.loads(raw_stripped)
        else:
            data = yaml.safe_load(raw_stripped)
    except Exception as err:
        raise SpecValidationError(f"Failed to parse {fmt.upper()} specification: {err}") from err

    if not isinstance(data, dict):
        raise SpecValidationError("Specification root must be a dictionary / mapping.")

    return data, fmt


def validate_spec_structure(spec: Dict[str, Any]) -> Tuple[List[str], List[int]]:
    """Validates spec against BlueAgent contract.

    Returns tuple of ([container_names], [ip_suffixes]).
    """
    chal_id = str(spec.get("challenge_id", "")).strip()
    if not chal_id or not RE_CHALLENGE_ID.match(chal_id):
        raise SpecValidationError(
            f"Invalid 'challenge_id': '{chal_id}'. Must be 1-25 lowercase alphanumeric characters or hyphens."
        )

    # Validate verifier
    verifier = spec.get("verifier")
    v_ip_suffix = 250
    if verifier is not None:
        if not isinstance(verifier, dict):
            raise SpecValidationError("'verifier' must be an object.")
        if not verifier.get("image"):
            raise SpecValidationError("'verifier.image' is required.")
        v_ip_suffix = verifier.get("ip_suffix", 250)
        if not (2 <= v_ip_suffix <= 254):
            raise SpecValidationError(f"'verifier.ip_suffix' ({v_ip_suffix}) must be between 2 and 254.")

    # Validate containers
    containers = spec.get("containers")
    if not isinstance(containers, list) or len(containers) == 0:
        raise SpecValidationError("Challenge must define at least one container in 'containers'.")

    container_names: List[str] = []
    ip_suffixes: List[int] = []
    container_map: Dict[str, int] = {}

    for idx, c in enumerate(containers):
        if not isinstance(c, dict):
            raise SpecValidationError(f"Container at index {idx} must be an object.")

        name = str(c.get("name", "")).strip()
        if not name or not RE_CONTAINER_NAME.match(name):
            raise SpecValidationError(
                f"Container {idx} has invalid name '{name}'. Must match ^[a-z0-9][a-z0-9-]{{0,20}}$."
            )

        if name in container_names:
            raise SpecValidationError(f"Duplicate container name '{name}' within challenge.")

        image = str(c.get("image", "")).strip()
        if not image:
            raise SpecValidationError(f"Container '{name}' must specify an 'image'.")

        ip_suffix = c.get("ip_suffix")
        if ip_suffix is None or not isinstance(ip_suffix, int) or not (2 <= ip_suffix <= 254):
            raise SpecValidationError(f"Container '{name}' has invalid 'ip_suffix': must be int between 2 and 254.")

        if ip_suffix == v_ip_suffix:
            raise SpecValidationError(
                f"Container '{name}' ip_suffix ({ip_suffix}) collides with verifier ip_suffix ({v_ip_suffix})."
            )

        if ip_suffix in ip_suffixes:
            raise SpecValidationError(
                f"Container '{name}' ip_suffix ({ip_suffix}) is already used by another container in this challenge."
            )

        mem_limit = c.get("limits_memory", "256MB")
        if not RE_LIMITS_MEM.match(str(mem_limit)):
            raise SpecValidationError(f"Container '{name}' invalid limits_memory: '{mem_limit}'. Expected e.g. 256MB, 1GB.")

        container_names.append(name)
        ip_suffixes.append(ip_suffix)
        container_map[name] = ip_suffix

    # Validate readiness commands
    readiness = spec.get("readiness_commands", [])
    if not isinstance(readiness, list):
        raise SpecValidationError("'readiness_commands' must be a list.")

    for idx, cmd in enumerate(readiness):
        _validate_command_spec(cmd, f"readiness_commands[{idx}]", container_names)

    # Validate verify commands
    verify_cmds = spec.get("verify_commands", [])
    if not isinstance(verify_cmds, list) or len(verify_cmds) == 0:
        raise SpecValidationError("'verify_commands' must contain at least one verification probe.")

    for idx, cmd in enumerate(verify_cmds):
        _validate_command_spec(cmd, f"verify_commands[{idx}]", container_names)

    # Validate template placeholders used across commands
    _validate_placeholders_in_spec(spec, set(container_names))

    return container_names, ip_suffixes


def _validate_command_spec(cmd: Any, prefix: str, valid_containers: List[str]) -> None:
    if not isinstance(cmd, dict):
        raise SpecValidationError(f"{prefix} must be an object.")

    target = cmd.get("target")
    if target not in ("container", "verifier"):
        raise SpecValidationError(f"{prefix}.target must be either 'container' or 'verifier'.")

    if target == "container":
        c_name = cmd.get("container_name")
        if not c_name or c_name not in valid_containers:
            raise SpecValidationError(
                f"{prefix}.container_name '{c_name}' does not match any container in 'containers' ({valid_containers})."
            )

    cmd_list = cmd.get("cmd")
    if not isinstance(cmd_list, list) or len(cmd_list) == 0:
        raise SpecValidationError(f"{prefix}.cmd must be a non-empty list of string tokens.")

    for token in cmd_list:
        if not isinstance(token, str):
            raise SpecValidationError(f"{prefix}.cmd elements must be strings, got {type(token).__name__}.")


def _validate_placeholders_in_spec(spec: Dict[str, Any], valid_containers: Set[str]) -> None:
    """Finds all {{placeholder}} patterns and validates their tokens."""
    text = json.dumps(spec)
    for match in RE_PLACEHOLDER.finditer(text):
        token = match.group(1).strip()
        if token in ("team_k", "subnet", "verifier_ip"):
            continue
        if token.startswith("ip:"):
            c_target = token[3:]
            if c_target not in valid_containers:
                raise SpecValidationError(
                    f"Invalid placeholder '{{{{{token}}}}}': container '{c_target}' is not defined in this challenge."
                )
        else:
            raise SpecValidationError(
                f"Unknown placeholder '{{{{{token}}}}}'. Allowed: {{{{team_k}}}}, {{{{subnet}}}}, {{{{verifier_ip}}}}, {{{{ip:<container_name>}}}}."
            )


def check_global_collisions(
    container_names: List[str],
    ip_suffixes: List[int],
    exclude_challenge_id: Optional[int] = None,
) -> None:
    """Checks whether container names or IP suffixes collide with any other BlueAgent challenge."""
    from .models import BlueAgentPatchChallenge

    query = BlueAgentPatchChallenge.query
    if exclude_challenge_id:
        query = query.filter(BlueAgentPatchChallenge.id != exclude_challenge_id)

    other_challenges = query.all()
    for other in other_challenges:
        other_names = set(other.parsed_container_names)
        other_ips = set(other.parsed_ip_suffixes)

        for name in container_names:
            if name in other_names:
                raise SpecValidationError(
                    f"Container name '{name}' conflicts with existing challenge '{other.name}' (ID {other.id}). "
                    "Container names must be globally unique across all BlueAgent challenges."
                )

        for ip_s in ip_suffixes:
            if ip_s in other_ips:
                raise SpecValidationError(
                    f"IP suffix {ip_s} conflicts with container in existing challenge '{other.name}' (ID {other.id}). "
                    "IP suffixes must be unique per team."
                )


def render_spec_for_team(spec: Dict[str, Any], team_k: int) -> Dict[str, Any]:
    """Renders a deep copy of the challenge spec tailored for a specific team_k.

    Replaces:
    - {{team_k}} -> str(team_k)
    - {{subnet}} -> 10.<team_k>.1
    - {{verifier_ip}} -> 10.<team_k>.1.<verifier.ip_suffix or 250>
    - {{ip:<name>}} -> 10.<team_k>.1.<ip_suffix>
    Sets:
    - spec['team_k'] = team_k
    """
    rendered = copy.deepcopy(spec)
    rendered["team_k"] = team_k

    # Compute container IP mappings
    c_ips: Dict[str, str] = {}
    for c in rendered.get("containers", []):
        c_name = c["name"]
        c_suffix = c["ip_suffix"]
        c_ips[c_name] = f"10.{team_k}.1.{c_suffix}"

    v_suffix = rendered.get("verifier", {}).get("ip_suffix", 250) if rendered.get("verifier") else 250
    verifier_ip = f"10.{team_k}.1.{v_suffix}"
    subnet = f"10.{team_k}.1"

    replacements: Dict[str, str] = {
        "team_k": str(team_k),
        "subnet": subnet,
        "verifier_ip": verifier_ip,
    }
    for c_name, ip_str in c_ips.items():
        replacements[f"ip:{c_name}"] = ip_str

    def _replace_string(s: str) -> str:
        def _sub_fn(m: re.Match) -> str:
            key = m.group(1).strip()
            return replacements.get(key, m.group(0))

        return RE_PLACEHOLDER.sub(_sub_fn, s)

    def _walk_and_replace(obj: Any) -> Any:
        if isinstance(obj, str):
            return _replace_string(obj)
        elif isinstance(obj, list):
            return [_walk_and_replace(item) for item in obj]
        elif isinstance(obj, dict):
            return {k: _walk_and_replace(v) for k, v in obj.items()}
        return obj

    # Replace placeholders in commands, env, etc.
    rendered["readiness_commands"] = _walk_and_replace(rendered.get("readiness_commands", []))
    rendered["verify_commands"] = _walk_and_replace(rendered.get("verify_commands", []))

    return rendered
