"""Configuration loader and settings resolver for the BlueAgent CTFd plugin.

Resolves settings in order of precedence:
1. CTFd dynamic configuration database (via `get_config`)
2. Environment variables
3. Hardcoded safe defaults
"""

import os
from typing import Optional


def get_plugin_config(key: str, default: Optional[str] = None) -> str:
    """Retrieves a configuration value from CTFd db settings, env var, or fallback."""
    # Try reading from CTFd's config store first (if CTFd is running and initialized)
    try:
        from CTFd.utils import get_config
        val = get_config(key)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    except Exception:
        pass

    # Fallback to uppercase environment variable (e.g. BLUEAGENT_URL)
    env_key = key.upper()
    env_val = os.getenv(env_key)
    if env_val is not None and env_val.strip() != "":
        return env_val.strip()

    return default if default is not None else ""


class BlueAgentConfig:
    """Plugin configuration properties."""

    @property
    def url(self) -> str:
        """Base URL for the BlueAgent REST API (e.g., http://127.0.0.1:8000 or http://172.17.0.1:8000)."""
        val = get_plugin_config("blueagent_url", "http://127.0.0.1:8000")
        return val.rstrip("/")

    @property
    def token(self) -> str:
        """API bearer token / CTFd token for BlueAgent authentication."""
        return get_plugin_config("blueagent_token", "secret-token")

    @property
    def default_verify_cooldown(self) -> int:
        """Default seconds cooldown between verification attempts per team."""
        try:
            return int(get_plugin_config("blueagent_verify_cooldown", "30"))
        except ValueError:
            return 30

    @property
    def connect_timeout(self) -> float:
        """HTTP connection timeout in seconds."""
        try:
            return float(get_plugin_config("blueagent_connect_timeout", "5.0"))
        except ValueError:
            return 5.0

    @property
    def verify_timeout(self) -> float:
        """HTTP read timeout in seconds for verification requests."""
        try:
            return float(get_plugin_config("blueagent_verify_timeout", "300.0"))
        except ValueError:
            return 300.0

    @property
    def deploy_timeout(self) -> float:
        """HTTP read timeout in seconds for container deploy requests."""
        try:
            return float(get_plugin_config("blueagent_deploy_timeout", "180.0"))
        except ValueError:
            return 180.0


settings = BlueAgentConfig()
