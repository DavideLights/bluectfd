"""Background worker and lifecycle daemon management for BlueAgent v2.

Manages background tasks responsible for state reconciliation,
self-healing orphaned sandbox resources, and graceful shutdown.
"""

import asyncio
from datetime import datetime, timezone
import logging
from typing import Optional
from ..config import settings
from ..engine.base import ContainerEngine

logger = logging.getLogger(__name__)


class LifecycleManager:
    """Manages the background reconciliation daemon lifecycle."""

    def __init__(
        self,
        engine: ContainerEngine,
        interval_seconds: int = settings.reconciliation_interval_seconds,
        grace_period_seconds: int = settings.resource_grace_period_seconds,
    ):
        self.engine = engine
        self.interval_seconds = interval_seconds
        self.grace_period_seconds = grace_period_seconds
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    @property
    def is_running(self) -> bool:
        """Indicates whether the reconciliation worker is actively running."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Starts the background reconciliation worker loop."""
        if self.is_running:
            logger.warning("LifecycleManager worker is already running.")
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._worker_loop(),
            name="blueagent-lifecycle-reconciliation",
        )
        logger.info(
            "LifecycleManager background worker started with interval=%ds, grace_period=%ds",
            self.interval_seconds,
            self.grace_period_seconds,
        )

    async def stop(self, timeout: float = 5.0) -> None:
        """Gracefully halts the background worker task with two-stage shutdown."""
        if not self.is_running or not self._task:
            return

        logger.info("Signaling LifecycleManager background worker to stop gracefully...")
        self._stop_event.set()

        try:
            # Stage 1: Give the current worker loop cycle up to `timeout` seconds to exit cleanly
            await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
        except asyncio.TimeoutError:
            # Stage 2: Hard cancel if uncooperative after timeout
            logger.warning("Worker did not terminate within %.1fs; enforcing cancellation.", timeout)
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            logger.info("LifecycleManager background worker stopped.")

    async def trigger_reconciliation(self) -> None:
        """Triggers a single reconciliation cycle immediately (on-demand)."""
        logger.debug("Manual reconciliation cycle triggered.")
        await self._run_reconciliation_cycle()

    async def _worker_loop(self) -> None:
        """Internal asynchronous event loop executing periodic reconciliation cycles."""
        while not self._stop_event.is_set():
            try:
                await self._run_reconciliation_cycle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error encountered during lifecycle reconciliation: %s", exc, exc_info=True)

            if self._stop_event.is_set():
                break

            try:
                # Sleep until next interval or immediate wake-up upon stop signal
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def _run_reconciliation_cycle(self) -> None:
        """Executes a single garbage collection cycle.

        1. Query Incus for instances prefixed with 'verify-'
        2. Filter out resources younger than self.grace_period_seconds
        3. Teardown and delete expired orphaned instances
        4. Query Incus OVN networks prefixed with 'verify-'
        5. Teardown and delete expired orphaned networks
        """
        now = datetime.now(timezone.utc)

        # 1. Clean up orphaned verify instances older than grace period
        try:
            verify_instances = await self.engine.list_instances(prefix="verify-")
            for inst_name in verify_instances:
                try:
                    meta = await self.engine.get_instance_metadata(inst_name)
                    if not meta:
                        continue

                    created_at_str = meta.get("created_at")
                    if created_at_str:
                        try:
                            created_dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                            age_seconds = (now - created_dt).total_seconds()
                            if age_seconds < self.grace_period_seconds:
                                continue
                        except Exception as dt_err:
                            logger.debug("Could not parse created_at for %s: %s", inst_name, dt_err)

                    logger.info(
                        "LifecycleManager: Pruning expired orphaned verification instance '%s'",
                        inst_name,
                    )
                    await self.engine.stop_instance(inst_name, force=True)
                    await self.engine.delete_instance(inst_name)
                except Exception as inst_err:
                    logger.warning(
                        "LifecycleManager: Failed cleaning up instance '%s': %s",
                        inst_name,
                        inst_err,
                    )
        except Exception as list_err:
            logger.warning("LifecycleManager: Error listing verification instances: %s", list_err)

        # 2. Clean up orphaned verify networks
        try:
            verify_nets = await self.engine.list_networks(prefix="verify-")
            for net_name in verify_nets:
                try:
                    logger.info(
                        "LifecycleManager: Pruning orphaned verification network '%s'",
                        net_name,
                    )
                    await self.engine.delete_network(net_name)
                except Exception as net_err:
                    logger.debug(
                        "LifecycleManager: Network '%s' could not be pruned (may still be in use): %s",
                        net_name,
                        net_err,
                    )
        except Exception as net_list_err:
            logger.warning("LifecycleManager: Error listing verification networks: %s", net_list_err)
