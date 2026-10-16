"""Inter-process and inter-coroutine synchronization primitives for BlueAgent v2.

Implements a Two-Tier Hierarchical Lock (asyncio.Lock + OS fcntl.flock):
- Tier 1 (Intra-Process): Serializes concurrent coroutines within the same Uvicorn worker process.
- Tier 2 (Inter-Process): Serializes multiple independent Uvicorn worker processes via kernel flock.
- Task Reentrancy: Allows nested lock acquisitions by the same asyncio.Task without self-deadlock.
"""

import asyncio
import os
import re
import time
from pathlib import Path
from typing import AsyncIterator, Dict, Optional
from contextlib import asynccontextmanager
from filelock import AsyncFileLock, Timeout

from .config import settings


class LockTimeoutError(Exception):
    """Raised when acquiring a resource lock exceeds the configured timeout."""

    def __init__(self, resource: str, timeout: float):
        self.resource = resource
        self.timeout = timeout
        super().__init__(
            f"Timed out after {timeout:.1f}s attempting to acquire exclusive lock on resource '{resource}'"
        )


class TwoTierLock:
    """Combines an in-process asyncio.Lock with an OS-level AsyncFileLock."""

    def __init__(
        self,
        lock_path: Path,
        resource_name: str,
        timeout: float = settings.default_lock_timeout,
    ):
        self.lock_path = lock_path
        self.resource_name = resource_name
        self.default_timeout = timeout
        self.file_lock = AsyncFileLock(str(lock_path), timeout=timeout)
        self.async_lock = asyncio.Lock()
        self._holder_task: Optional[asyncio.Task] = None
        self._depth = 0

    @property
    def is_locked(self) -> bool:
        """Indicates whether this lock is currently held by a task in this process."""
        return self._holder_task is not None

    async def acquire(self, timeout: Optional[float] = None) -> None:
        """Acquires the two-tier lock respecting total timeout."""
        actual_timeout = timeout if timeout is not None else self.default_timeout
        cur_task = asyncio.current_task()

        # Task-level reentrancy within the same coroutine execution stack
        if self._holder_task is not None and self._holder_task == cur_task:
            self._depth += 1
            return

        deadline = time.monotonic() + actual_timeout

        # Tier 1: Intra-process coroutine serialization
        wait_for_coro = max(0.0, deadline - time.monotonic())
        try:
            await asyncio.wait_for(self.async_lock.acquire(), timeout=wait_for_coro)
        except asyncio.TimeoutError as err:
            raise LockTimeoutError(resource=self.resource_name, timeout=actual_timeout) from err

        # Tier 2: Inter-process kernel file lock serialization
        wait_for_file = max(0.0, deadline - time.monotonic())
        self.file_lock.timeout = wait_for_file
        try:
            await self.file_lock.acquire()
            self._holder_task = cur_task
            self._depth = 1
        except (Timeout, asyncio.TimeoutError) as err:
            self.async_lock.release()
            raise LockTimeoutError(resource=self.resource_name, timeout=actual_timeout) from err
        except Exception:
            self.async_lock.release()
            raise

    async def release(self) -> None:
        """Releases the lock when depth reaches zero."""
        cur_task = asyncio.current_task()
        if self._holder_task != cur_task:
            return

        self._depth -= 1
        if self._depth <= 0:
            self._depth = 0
            self._holder_task = None
            try:
                await self.file_lock.release()
            finally:
                if self.async_lock.locked():
                    self.async_lock.release()

    async def __aenter__(self) -> "TwoTierLock":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release()


class LockRegistry:
    """Registry managing granular, cross-process and cross-coroutine OS file locks.

    Ensures that concurrent API requests across different Uvicorn worker processes
    do not trigger race conditions or HTTP 409 conflicts on shared Incus instances,
    host network namespaces, or WireGuard configurations.
    """

    def __init__(self, lock_dir: str = settings.lock_directory):
        self.lock_dir = Path(lock_dir)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._locks: Dict[Path, TwoTierLock] = {}

    def _sanitize_key(self, raw_key: str) -> str:
        """Sanitizes an arbitrary identifier into a safe filename."""
        sanitized = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", str(raw_key))
        return sanitized.strip("_") or "default"

    def _get_lock_path(self, resource_type: str, resource_id: str) -> Path:
        """Computes the full filesystem path for the lock file."""
        safe_type = self._sanitize_key(resource_type)
        safe_id = self._sanitize_key(resource_id)
        return self.lock_dir / f"{safe_type}_{safe_id}.lock"

    def get_lock(
        self,
        resource_type: str,
        resource_id: str,
        timeout: float = settings.default_lock_timeout,
    ) -> TwoTierLock:
        """Returns or creates a cached TwoTierLock instance for the specified resource."""
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._get_lock_path(resource_type, resource_id)
        resource_name = f"{resource_type}:{resource_id}"
        if lock_path not in self._locks:
            self._locks[lock_path] = TwoTierLock(lock_path, resource_name=resource_name, timeout=timeout)
        else:
            self._locks[lock_path].default_timeout = timeout
            self._locks[lock_path].resource_name = resource_name
        return self._locks[lock_path]

    def is_locked(self, resource_type: str, resource_id: str) -> bool:
        """Checks if the lock for the given resource is currently held by this process."""
        lock_path = self._get_lock_path(resource_type, resource_id)
        lock = self._locks.get(lock_path)
        return lock is not None and lock.is_locked

    @asynccontextmanager
    async def acquire(
        self,
        resource_type: str,
        resource_id: str,
        timeout: float = settings.default_lock_timeout,
    ) -> AsyncIterator[None]:
        """Async context manager that acquires and releases a resource lock.

        Raises:
            LockTimeoutError: If the lock cannot be acquired within the timeout period.
        """
        lock = self.get_lock(resource_type, resource_id, timeout=timeout)
        await lock.acquire(timeout=timeout)
        try:
            yield
        finally:
            await lock.release()

    @asynccontextmanager
    async def lock_team(
        self,
        team_k: int | str,
        timeout: float = settings.default_lock_timeout,
    ) -> AsyncIterator[None]:
        """Convenience context manager for serializing mutations on a specific team domain."""
        async with self.acquire("team", str(team_k), timeout=timeout):
            yield

    @asynccontextmanager
    async def lock_instance(
        self,
        instance_name: str,
        timeout: float = settings.default_lock_timeout,
    ) -> AsyncIterator[None]:
        """Convenience context manager for serializing mutations on an Incus instance."""
        async with self.acquire("instance", instance_name, timeout=timeout):
            yield

    @asynccontextmanager
    async def lock_global(
        self,
        name: str = "global",
        timeout: float = 60.0,
    ) -> AsyncIterator[None]:
        """Convenience context manager for global host-level operations (e.g. nftables rebuilds)."""
        async with self.acquire("global", name, timeout=timeout):
            yield
