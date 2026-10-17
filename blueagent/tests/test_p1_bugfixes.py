import pytest
from unittest.mock import AsyncMock, MagicMock
from blueagent.models import CommandSpec, ChallengeConfig
from blueagent.verifier.ovn import OVNVerifier

@pytest.mark.asyncio
async def test_command_spec_retries_and_delay():
    cmd = CommandSpec(cmd=["true"], retries=3, retry_delay=0.1)
    assert cmd.retries == 3
    assert cmd.retry_delay == 0.1

    cmd_default = CommandSpec(cmd=["true"])
    assert cmd_default.retries == 1
    assert cmd_default.retry_delay == 1.0

@pytest.mark.asyncio
async def test_ovn_verifier_internet_access_flag():
    mock_engine = AsyncMock()
    mock_engine.create_network = AsyncMock()
    mock_engine.clone_instance = AsyncMock()
    mock_engine.connect_network = AsyncMock()
    mock_engine.start_instance = AsyncMock()
    mock_engine.exec_command = AsyncMock()
    mock_engine.delete_network = AsyncMock()
    mock_engine.stop_instance = AsyncMock()
    mock_engine.delete_instance = AsyncMock()

    cfg_no_inet = ChallengeConfig(
        challenge_id="test-no-inet",
        verify_internet_access=False,
        containers=[],
        verify_commands=[],
    )

    async with OVNVerifier(engine=mock_engine, config=cfg_no_inet, team_k=42):
        pass

    assert mock_engine.create_network.called
    call_args = mock_engine.create_network.call_args[1]
    assert call_args["name"] == "verify-42"
    assert call_args["config"]["ipv4.nat"] == "false"
    assert "network" not in call_args["config"]
