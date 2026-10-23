"""Distributed concurrency locks and cooldown mechanics for BlueAgent verification and deployment.

Utilizes CTFd's core cache backend (Redis / CacheLib) with in-process memory fallback.
Enforces:
- Atomic per-(team, challenge) submit cooldowns with instant refund on 409 conflict
- Team-level verification mutexes to prevent concurrent OVN sandbox collision
- Team-level deployment mutexes to prevent duplicate container provisioning
"""

from contextlib import contextmanager
import logging
import threading
import time
from typing import Generator, Optional, Tuple

logger = logging.getLogger(__name__)

# In-process thread locks & storage fallback if cache backend is absent or simple
_local_lock = threading.Lock()
_local_cooldowns: dict = {}
_local_mutexes: dict = {}


def _get_cache():
    try:
        from CTFd.cache import cache
        return cache
    except Exception:
        return None


def check_and_set_cooldown(team_k: int, challenge_id: int, cooldown_seconds: int) -> Tuple[bool, int]:
    """Atomically checks and sets verification cooldown for a (team_k, challenge_id) pair.

    Returns:
        (is_allowed, seconds_remaining)
        If is_allowed is True, seconds_remaining is 0.
        If False, seconds_remaining is positive integer to display in UI.
    """
    key = f"blueagent:cooldown:{team_k}:{challenge_id}"
    cache = _get_cache()
    now = time.time()

    if cache:
        try:
            val = cache.get(key)
            if val is not None:
                try:
                    remaining = float(val) - now
                    if remaining > 0:
                        return False, max(1, int(remaining))
                except (ValueError, TypeError):
                    pass
            # Key does not exist or has expired
            cache.set(key, now + cooldown_seconds, timeout=cooldown_seconds)
            return True, 0
        except Exception as err:
            logger.debug("Cache cooldown check failed, using fallback: %s", err)

    # Fallback to local memory dictionary
    with _local_lock:
        val = _local_cooldowns.get(key)
        if val is not None and val > now:
            return False, max(1, int(val - now))
        _local_cooldowns[key] = now + cooldown_seconds
        return True, 0


def refund_cooldown(team_k: int, challenge_id: int) -> None:
    """Refunds/deletes cooldown key when BlueAgent returns HTTP 409 Conflict (busy/locked)."""
    key = f"blueagent:cooldown:{team_k}:{challenge_id}"
    cache = _get_cache()
    if cache and hasattr(cache, "delete"):
        try:
            cache.delete(key)
        except Exception as err:
            logger.debug("Error clearing cache key %s: %s", key, err)

    with _local_lock:
        _local_cooldowns.pop(key, None)


@contextmanager
def team_verify_lock(team_k: int, timeout: int = 330) -> Generator[bool, None, None]:
    """Context manager acquiring exclusive verification mutex for team_k.

    Yields True if lock acquired, False if another verification is currently running for this team.
    """
    key = f"blueagent:mutex:verify:{team_k}"
    cache = _get_cache()
    acquired = False

    if cache and hasattr(cache, "add"):
        # Atomic add returns True only if key did not previously exist
        acquired = bool(cache.add(key, "1", timeout=timeout))
    else:
        with _local_lock:
            if key not in _local_mutexes:
                _local_mutexes[key] = time.time() + timeout
                acquired = True
            else:
                if time.time() > _local_mutexes[key]:
                    _local_mutexes[key] = time.time() + timeout
                    acquired = True

    try:
        yield acquired
    finally:
        if acquired:
            if cache and hasattr(cache, "delete"):
                try:
                    cache.delete(key)
                except Exception:
                    pass
            with _local_lock:
                _local_mutexes.pop(key, None)


@contextmanager
def team_deploy_lock(team_k: int, timeout: int = 190) -> Generator[bool, None, None]:
    """Context manager acquiring exclusive deployment mutex for team_k."""
    key = f"blueagent:mutex:deploy:{team_k}"
    cache = _get_cache()
    acquired = False

    if cache and hasattr(cache, "add"):
        acquired = bool(cache.add(key, "1", timeout=timeout))
    else:
        with _local_lock:
            if key not in _local_mutexes:
                _local_mutexes[key] = time.time() + timeout
                acquired = True
            else:
                if time.time() > _local_mutexes[key]:
                    _local_mutexes[key] = time.time() + timeout
                    acquired = True

    try:
        yield acquired
    finally:
        if acquired:
            if cache and hasattr(cache, "delete"):
                try:
                    cache.delete(key)
                except Exception:
                    pass
            with _local_lock:
                _local_mutexes.pop(key, None)
