"""Comprehensive tests for LockRegistry cross-process synchronization."""

import asyncio
import time
import pytest
from blueagent.locks import LockRegistry, LockTimeoutError


@pytest.mark.asyncio
async def test_lock_basic_acquire(tmp_path):
    registry = LockRegistry(lock_dir=str(tmp_path))

    acquired = False
    async with registry.lock_team(team_k=1, timeout=2.0):
        acquired = True
        assert registry.is_locked("team", "1") is True

    assert acquired is True
    assert registry.is_locked("team", "1") is False


@pytest.mark.asyncio
async def test_lock_coroutine_reentrancy(tmp_path):
    """Verifies that nested lock acquisitions on the same resource within the same process do not self-deadlock."""
    registry = LockRegistry(lock_dir=str(tmp_path))
    steps = []

    async with registry.lock_team(team_k=7, timeout=1.0):
        steps.append("outer_entered")
        assert registry.is_locked("team", "7") is True
        async with registry.lock_team(team_k=7, timeout=1.0):
            steps.append("inner_entered")
            assert registry.is_locked("team", "7") is True
        steps.append("inner_exited")

    steps.append("outer_exited")
    assert steps == ["outer_entered", "inner_entered", "inner_exited", "outer_exited"]
    assert registry.is_locked("team", "7") is False


@pytest.mark.asyncio
async def test_lock_serialization_same_resource(tmp_path):
    registry = LockRegistry(lock_dir=str(tmp_path))
    order = []

    async def worker_1():
        async with registry.lock_team(team_k=42, timeout=5.0):
            order.append("worker1_start")
            await asyncio.sleep(0.1)
            order.append("worker1_end")

    async def worker_2():
        await asyncio.sleep(0.02)  # Ensure worker 1 acquires first
        # Different key representation to test sanitization consistency
        async with registry.acquire("team", "42", timeout=5.0):
            order.append("worker2_acquired")

    await asyncio.gather(worker_1(), worker_2())

    assert order == ["worker1_start", "worker1_end", "worker2_acquired"]


@pytest.mark.asyncio
async def test_lock_independent_resources_parallel(tmp_path):
    registry = LockRegistry(lock_dir=str(tmp_path))
    timestamps = {}

    async def worker_team1():
        async with registry.lock_team(team_k=1, timeout=5.0):
            timestamps["t1_start"] = time.monotonic()
            await asyncio.sleep(0.1)
            timestamps["t1_end"] = time.monotonic()

    async def worker_team2():
        async with registry.lock_team(team_k=2, timeout=5.0):
            timestamps["t2_start"] = time.monotonic()
            await asyncio.sleep(0.1)
            timestamps["t2_end"] = time.monotonic()

    await asyncio.gather(worker_team1(), worker_team2())

    # Because team 1 and team 2 use separate locks, team 2 should start before team 1 ends
    assert timestamps["t2_start"] < timestamps["t1_end"]


@pytest.mark.asyncio
async def test_lock_timeout_error(tmp_path):
    registry_a = LockRegistry(lock_dir=str(tmp_path))
    # Simulate a second worker process by using a separate registry instance with independent cache
    registry_b = LockRegistry(lock_dir=str(tmp_path))

    async def blocker():
        async with registry_a.lock_instance("inst-1", timeout=5.0):
            await asyncio.sleep(0.5)

    async def blocked():
        await asyncio.sleep(0.05)
        # Timeout set to 0.1s should fail because blocker holds for 0.5s
        with pytest.raises(LockTimeoutError) as exc_info:
            async with registry_b.lock_instance("inst-1", timeout=0.1):
                pass
        assert "Timed out" in str(exc_info.value)
        assert "instance:inst-1" in str(exc_info.value)

    await asyncio.gather(blocker(), blocked())


@pytest.mark.asyncio
async def test_lock_global_helper(tmp_path):
    registry = LockRegistry(lock_dir=str(tmp_path))
    async with registry.lock_global("nftables-rebuild", timeout=2.0):
        assert registry.is_locked("global", "nftables-rebuild") is True
    assert registry.is_locked("global", "nftables-rebuild") is False
