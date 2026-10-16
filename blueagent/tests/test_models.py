"""Tests for Pydantic v2 schemas and validation models."""

import pytest
from pydantic import ValidationError
from blueagent.models import (
    ChallengeConfig,
    CommandSpec,
    ContainerSpec,
    ExecResult,
    TeamCreateReq,
    TeamMemberCreateReq,
    VerifierSpec,
    VerifyResultResp,
)


def test_exec_result_instantiation():
    res = ExecResult(exit_code=0, stdout="hello", stderr="")
    assert res.exit_code == 0
    assert res.stdout == "hello"
    assert res.stderr == ""


def test_container_spec_validation():
    # Valid spec
    spec = ContainerSpec(
        name="web-01",
        image="images:debian/12",
        ip_suffix=10,
        limits_cpu="2",
        limits_memory="512MB",
    )
    assert spec.name == "web-01"
    assert spec.ip_suffix == 10

    # Invalid ip_suffix (1 is reserved for gateway)
    with pytest.raises(ValidationError):
        ContainerSpec(name="web-01", image="images:debian/12", ip_suffix=1)

    # Invalid ip_suffix (255 is broadcast)
    with pytest.raises(ValidationError):
        ContainerSpec(name="web-01", image="images:debian/12", ip_suffix=255)


def test_command_spec_defaults():
    cmd = CommandSpec(cmd=["curl", "-s", "http://10.1.1.2:80"])
    assert cmd.target == "container"
    assert cmd.timeout == 15
    assert cmd.env == {}

    # Empty command list should raise
    with pytest.raises(ValidationError):
        CommandSpec(cmd=[])


def test_challenge_config_full():
    cfg = ChallengeConfig(
        challenge_id="sql-injection-101",
        verify_internet_access=False,
        verifier=VerifierSpec(ip_suffix=250),
        containers=[
            ContainerSpec(name="app", image="images:ubuntu/22.04", ip_suffix=2),
            ContainerSpec(name="db", image="images:ubuntu/22.04", ip_suffix=3),
        ],
        verify_commands=[
            CommandSpec(
                target="verifier",
                cmd=["curl", "--fail", "http://10.5.1.2:8000/health"],
            ),
            CommandSpec(
                target="container",
                container_name="app",
                cmd=["pgrep", "python3"],
            ),
        ],
    )
    assert cfg.challenge_id == "sql-injection-101"
    assert len(cfg.containers) == 2
    assert len(cfg.verify_commands) == 2
    assert cfg.verifier is not None
    assert cfg.verifier.ip_suffix == 250


def test_team_models():
    from blueagent.models import TeamPutReq, TeamMemberPutReq

    req = TeamCreateReq(team_k=1, name="Team Alpha")
    assert req.team_k == 1
    assert req.name == "Team Alpha"

    put_req = TeamPutReq(name="Team Alpha")
    assert put_req.name == "Team Alpha"

    with pytest.raises(ValidationError):
        TeamCreateReq(team_k=-1, name="Invalid Team")

    with pytest.raises(ValidationError):
        TeamCreateReq(team_k=255, name="Invalid Team")

    member_req = TeamMemberCreateReq(name="Alice")
    assert member_req.name == "Alice"
    assert member_req.public_key is None

    member_put = TeamMemberPutReq(name="Bob", public_key="pubkey123")
    assert member_put.name == "Bob"
    assert member_put.public_key == "pubkey123"
