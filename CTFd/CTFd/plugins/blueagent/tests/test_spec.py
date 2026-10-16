"""Unit tests for specification parsing, validation, collision detection, and templating."""

import pytest

from CTFd.plugins.blueagent.spec import (
    SpecValidationError,
    parse_spec_text,
    render_spec_for_team,
    validate_spec_structure,
)

SAMPLE_VALID_YAML = """
challenge_id: "patch-web-01"
verifier:
  image: "curlimages/curl:latest"
  ip_suffix: 250
  limits_cpu: "1"
  limits_memory: "256MB"
containers:
  - name: "web"
    image: "alpine-3.20-nginx"
    ip_suffix: 10
    limits_cpu: "1"
    limits_memory: "512MB"
  - name: "db"
    image: "alpine-3.20-mariadb"
    ip_suffix: 11
    limits_cpu: "1"
    limits_memory: "256MB"
readiness_commands:
  - target: "container"
    container_name: "web"
    cmd: ["pgrep", "nginx"]
    timeout: 5
    interval: 2
    retry_timeout: 15
verify_commands:
  - target: "verifier"
    cmd: ["curl", "-sf", "http://{{ip:web}}:80/login"]
    timeout: 10
  - target: "verifier"
    cmd: ["nc", "-z", "{{ip:db}}", "3306"]
    timeout: 5
"""


def test_parse_valid_yaml():
    data, fmt = parse_spec_text(SAMPLE_VALID_YAML)
    assert fmt == "yaml"
    assert data["challenge_id"] == "patch-web-01"
    assert len(data["containers"]) == 2


def test_validate_spec_structure_valid():
    data, _ = parse_spec_text(SAMPLE_VALID_YAML)
    c_names, ip_suffixes = validate_spec_structure(data)
    assert c_names == ["web", "db"]
    assert ip_suffixes == [10, 11]


def test_validate_spec_duplicate_container_name():
    bad_spec = {
        "challenge_id": "test-chal",
        "containers": [
            {"name": "web", "image": "img1", "ip_suffix": 10},
            {"name": "web", "image": "img2", "ip_suffix": 11},
        ],
        "verify_commands": [{"target": "verifier", "cmd": ["true"]}],
    }
    with pytest.raises(SpecValidationError, match="Duplicate container name 'web'"):
        validate_spec_structure(bad_spec)


def test_validate_spec_ip_suffix_collision_with_verifier():
    bad_spec = {
        "challenge_id": "test-chal",
        "verifier": {"image": "curl:latest", "ip_suffix": 250},
        "containers": [
            {"name": "web", "image": "img1", "ip_suffix": 250},
        ],
        "verify_commands": [{"target": "verifier", "cmd": ["true"]}],
    }
    with pytest.raises(SpecValidationError, match="collides with verifier ip_suffix"):
        validate_spec_structure(bad_spec)


def test_validate_spec_unknown_placeholder():
    bad_spec = {
        "challenge_id": "test-chal",
        "containers": [
            {"name": "web", "image": "img1", "ip_suffix": 10},
        ],
        "verify_commands": [
            {"target": "verifier", "cmd": ["curl", "http://{{ip:nonexistent}}/test"]},
        ],
    }
    with pytest.raises(SpecValidationError, match="container 'nonexistent' is not defined"):
        validate_spec_structure(bad_spec)


def test_render_spec_for_team():
    data, _ = parse_spec_text(SAMPLE_VALID_YAML)
    team_k = 7
    rendered = render_spec_for_team(data, team_k=team_k)

    assert rendered["team_k"] == 7
    verify_cmds = rendered["verify_commands"]
    # Check that {{ip:web}} was rendered to 10.7.1.10
    assert verify_cmds[0]["cmd"] == ["curl", "-sf", "http://10.7.1.10:80/login"]
    # Check that {{ip:db}} was rendered to 10.7.1.11
    assert verify_cmds[1]["cmd"] == ["nc", "-z", "10.7.1.11", "3306"]
