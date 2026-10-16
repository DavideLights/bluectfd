"""Global configuration and environment management for BlueAgent v2.

Adheres to 12-factor application methodology via Pydantic BaseSettings,
supporting overrides via environment variables (prefixed with BLUEAGENT_).
"""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration settings for BlueAgent orchestration engine."""

    model_config = SettingsConfigDict(
        env_prefix="BLUEAGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # API Server Configuration
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    log_level: str = "INFO"

    # Incus (LXC) Configuration
    incus_socket_path: str = "/var/lib/incus/unix.socket"
    incus_api_version: str = "1.0"
    incus_base_url: str = "http://localhost/1.0"
    incus_uplink_network: str = "incusbr0"
    incus_operation_timeout: int = 240

    # Storage and Database Configuration
    database_path: str = "blueagent.db"

    # Concurrency and Inter-Process Locking
    lock_directory: str = "/tmp/blueagent_locks"
    default_lock_timeout: float = 30.0

    # Lifecycle and Self-Healing Daemon
    reconciliation_interval_seconds: int = 120
    resource_grace_period_seconds: int = 600

    # WireGuard and Host Network Configuration
    wireguard_config_dir: str = "/etc/wireguard"
    host_bridge_prefix: str = "br-team-"
    wg_interface_prefix: str = "wg-"
    wg_base_port: int = 51820
    wg_mtu: int = 1420
    wg_keepalive: int = 25
    wg_public_endpoint: str = "127.0.0.1"
    nft_table: str = "blueagent"

    # CTFd Integration & Security
    ctfd_api_token: str = "secret-token"

    # Image Building & Distrobuilder
    image_build_dir: str = "/var/lib/blueagent/builds"
    image_build_timeout: int = 1800
    image_yaml_max_bytes: int = 262144
    image_archive_max_bytes: int = 104857600
    distrobuilder_bin: str = "distrobuilder"
    use_sudo_distrobuilder: bool = True
    readiness_retry_interval: float = 2.0


settings = Settings()
