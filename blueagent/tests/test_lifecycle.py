"""Comprehensive tests for LifecycleManager background daemon management."""

import asyncio
import pytest
from unittest.mock import AsyncMock
from blueagent.lifecycle.manager import LifecycleManager


@pytest.mark.asyncio
async def test_lifecycle_manager_start_and_stop():
    mock_engine = AsyncMock()
    mgr = LifecycleManager(
        engine=mock_engine,
        interval_seconds=1,
        grace_period_seconds=10,
    )

    assert mgr.is_running is False

    await mgr.start()
    assert mgr.is_running is True

    # Let worker cycle run
    await asyncio.sleep(0.05)

    # Stop gracefully
    await mgr.stop(timeout=2.0)
    assert mgr.is_running is False


@pytest.mark.asyncio
async def test_lifecycle_manager_idempotent_operations():
    mock_engine = AsyncMock()
    mgr = LifecycleManager(engine=mock_engine, interval_seconds=1)

    # Double start should not crash
    await mgr.start()
    await mgr.start()
    assert mgr.is_running is True

    # Double stop should not crash
    await mgr.stop()
    await mgr.stop()
    assert mgr.is_running is False


@pytest.mark.asyncio
async def test_lifecycle_manager_trigger_reconciliation():
    mock_engine = AsyncMock()
    mgr = LifecycleManager(engine=mock_engine, interval_seconds=60)

    # Trigger single manual cycle without starting continuous loop
    assert mgr.is_running is False
    await mgr.trigger_reconciliation()
    assert mgr.is_running is False


@pytest.mark.asyncio
async def test_lifecycle_manager_graceful_stop_wakes_immediately():
    """Verifies that calling stop() immediately wakes up the sleeping worker without waiting for full interval."""
    mock_engine = AsyncMock()
    # Large interval of 100 seconds
    mgr = LifecycleManager(engine=mock_engine, interval_seconds=100)

    await mgr.start()
    await asyncio.sleep(0.05)  # Allow worker to enter wait

    start_t = asyncio.get_event_loop().time()
    await mgr.stop(timeout=2.0)
    duration = asyncio.get_event_loop().time() - start_t

    # Must exit promptly (< 0.5s), not wait 100s
    assert duration < 0.5
    assert mgr.is_running is False


@pytest.mark.asyncio
async def test_lifecycle_manager_reconciliation_prunes_expired():
    """Verifies that instances older than grace period and orphaned networks are pruned."""
    mock_engine = AsyncMock()
    mock_engine.list_instances.return_value = ["verify-1-web", "verify-1-runner"]
    mock_engine.get_instance_metadata.side_effect = lambda name: {
        "name": name,
        "created_at": "2020-01-01T00:00:00Z",  # Well in the past
    }
    mock_engine.list_networks.return_value = ["verify-1"]

    mgr = LifecycleManager(engine=mock_engine, grace_period_seconds=90)
    await mgr.trigger_reconciliation()

    # Both expired instances should be stopped and deleted
    assert mock_engine.stop_instance.call_count == 2
    assert mock_engine.delete_instance.call_count == 2
    mock_engine.delete_network.assert_called_once_with("verify-1")


@pytest.mark.asyncio
async def test_lifecycle_manager_reconciliation_preserves_fresh():
    """Verifies that fresh instances younger than grace period are NOT pruned."""
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()

    mock_engine = AsyncMock()
    mock_engine.list_instances.return_value = ["verify-2-web"]
    mock_engine.get_instance_metadata.return_value = {
        "name": "verify-2-web",
        "created_at": now_iso,
    }
    mock_engine.list_networks.return_value = []

    mgr = LifecycleManager(engine=mock_engine, grace_period_seconds=90)
    await mgr.trigger_reconciliation()

    mock_engine.stop_instance.assert_not_called()
    mock_engine.delete_instance.assert_not_called()
