"""WireGuard L3 interface and peer cryptokey manager for BlueAgent v2.

Manages dedicated wg-k kernel interfaces, peer cryptokey routing bindings,
server key storage, and per-member client configuration generation.
"""

import base64
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives.asymmetric import x25519

from ..config import Settings, settings
from ._runner import CommandRunner, SubprocessRunner
from .addressing import TeamNetPlan, plan_for
from .commands import ip, wg
from .templates import render_wireguard_client_conf

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemberCredentials:
    """WireGuard cryptographic credentials and client configuration file payload."""

    name: str
    client_ip: str
    public_key: str
    private_key: Optional[str]
    preshared_key: Optional[str]
    config_text: str
    server_endpoint: str
    server_public_key: str


def generate_keypair() -> tuple[str, str]:
    """Generates a Curve25519 keypair for WireGuard using cryptography X25519.

    Returns:
        Tuple of (private_key_b64, public_key_b64).
    """
    priv = x25519.X25519PrivateKey.generate()
    priv_bytes = priv.private_bytes_raw()
    pub_bytes = priv.public_key().public_bytes_raw()
    return (
        base64.b64encode(priv_bytes).decode("ascii"),
        base64.b64encode(pub_bytes).decode("ascii"),
    )


def public_key_from_private(private_key_b64: str) -> str:
    """Derives a base64-encoded WireGuard public key from a private key."""
    raw_priv = base64.b64decode(private_key_b64.strip())
    priv = x25519.X25519PrivateKey.from_private_bytes(raw_priv)
    pub_bytes = priv.public_key().public_bytes_raw()
    return base64.b64encode(pub_bytes).decode("ascii")


def generate_preshared_key() -> str:
    """Generates 32 bytes of cryptographically secure random data for WireGuard PSK."""
    return base64.b64encode(os.urandom(32)).decode("ascii")


class WireGuardManager:
    """Manages kernel-space WireGuard interfaces, peer cryptokeys, and client configuration."""

    def __init__(
        self,
        interface_prefix: str = settings.wg_interface_prefix,
        runner: Optional[CommandRunner] = None,
        config: Settings = settings,
    ):
        self.interface_prefix = interface_prefix
        self.runner = runner or SubprocessRunner()
        self.settings = config
        self.config_dir = Path(config.wireguard_config_dir)

    def _get_key_file_path(self, iface_name: str) -> Path:
        """Returns the filesystem path for the server private key."""
        return self.config_dir / f"{iface_name}.key"

    def _ensure_server_private_key(self, iface_name: str) -> str:
        """Loads the persisted server private key or securely creates a new one (mode 0600)."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        key_path = self._get_key_file_path(iface_name)

        if key_path.exists():
            try:
                priv = key_path.read_text().strip()
                if priv:
                    return priv
            except Exception as err:
                logger.warning("Failed reading existing key file %s: %s; generating new key", key_path, err)

        priv_key, _ = generate_keypair()

        # Atomic write with 0600 permissions
        with tempfile.NamedTemporaryFile("w", dir=str(self.config_dir), delete=False) as tf:
            os.chmod(tf.name, 0o600)
            tf.write(priv_key + "\n")
            temp_path = Path(tf.name)

        temp_path.replace(key_path)
        logger.info("Generated and stored new server private key at '%s'", key_path)
        return priv_key

    async def setup_team_interface(self, team_k: int, listen_port: Optional[int] = None) -> str:
        """Initializes the wg-k interface on subnet 10.k.0.1/24.

        Provisions the link, sets the private key and listen port, assigns the IP,
        sets the MTU, and brings the interface UP.

        Args:
            team_k: Team identifier index k.
            listen_port: Optional UDP listen port override (default: 51820 + k).

        Returns:
            The created WireGuard interface name.
        """
        plan = plan_for(team_k, wg_prefix=self.interface_prefix, base_port=self.settings.wg_base_port)
        port = listen_port if listen_port is not None else plan.listen_port
        iface = plan.wg_iface

        logger.info("Setting up WireGuard interface '%s' (port %d) for team %d", iface, port, team_k)

        # 1. Check if interface exists
        check = await self.runner.run(
            ip.link_show(iface, json=True),
            check=False,
        )
        if check.returncode != 0:
            logger.debug("Creating WireGuard interface '%s'", iface)
            await self.runner.run(
                ip.link_add_wireguard(iface),
                check=True,
            )

        # 2. Retrieve or generate server private key
        server_priv = self._ensure_server_private_key(iface)

        # 3. Configure port and private key via stdin (prevents secret leakage on argv)
        await self.runner.run(
            wg.set_listen_port_and_key(iface, port, key_via_stdin=True),
            stdin=server_priv.strip().encode("utf-8") + b"\n",
            check=True,
        )

        # 4. Set IP address
        await self.runner.run(
            ip.addr_replace(plan.wg_server_cidr, iface),
            check=True,
        )

        # 5. Set MTU (1420 bytes default to prevent PMTU blackholes)
        await self.runner.run(
            ip.link_set_mtu(iface, self.settings.wg_mtu),
            check=True,
        )

        # 6. Bring interface up
        await self.runner.run(
            ip.link_up(iface),
            check=True,
        )

        return iface

    async def teardown_team_interface(self, team_k: int) -> None:
        """Brings down and deletes the wg-k interface and removes its private key."""
        plan = plan_for(team_k, wg_prefix=self.interface_prefix)
        iface = plan.wg_iface
        logger.info("Tearing down WireGuard interface '%s' for team %d", iface, team_k)

        # 1. Set interface down
        await self.runner.run(
            ip.link_down(iface),
            check=False,
        )

        # 2. Delete interface
        await self.runner.run(
            ip.link_delete(iface),
            check=False,
        )

        # 3. Delete persisted key file
        key_path = self._get_key_file_path(iface)
        if key_path.exists():
            try:
                key_path.unlink()
            except OSError as err:
                logger.warning("Failed removing key file '%s': %s", key_path, err)

    async def add_peer(
        self,
        team_k: int,
        member_id: str,
        public_key: str,
        allowed_ips: str,
        preshared_key: Optional[str] = None,
    ) -> None:
        """Registers a participant public key and AllowedIPs on wg-k.

        Args:
            team_k: Team identifier index k.
            member_id: Unique member identifier (for logging).
            public_key: Participant Curve25519 public key.
            allowed_ips: Authorized client IP address or CIDR (e.g. 10.k.0.2/32).
            preshared_key: Optional post-quantum preshared symmetric key.
        """
        plan = plan_for(team_k, wg_prefix=self.interface_prefix)
        iface = plan.wg_iface
        target_allowed = allowed_ips if "/" in allowed_ips else f"{allowed_ips}/32"

        logger.info(
            "Adding peer '%s' (pubkey: %s...) with allowed-ips %s on %s",
            member_id,
            public_key[:8],
            target_allowed,
            iface,
        )

        cmd = wg.set_peer(
            iface,
            public_key,
            target_allowed,
            psk_via_stdin=bool(preshared_key),
        )
        stdin_bytes: Optional[bytes] = None

        if preshared_key:
            stdin_bytes = preshared_key.strip().encode("utf-8") + b"\n"

        await self.runner.run(cmd, stdin=stdin_bytes, check=True)

    async def remove_peer(self, team_k: int, public_key: str) -> None:
        """Removes a peer from wg-k by public key."""
        plan = plan_for(team_k, wg_prefix=self.interface_prefix)
        iface = plan.wg_iface
        logger.info("Removing peer (pubkey: %s...) from %s", public_key[:8], iface)

        await self.runner.run(
            wg.remove_peer(iface, public_key),
            check=False,
        )

    async def get_server_public_key(self, team_k: int) -> str:
        """Retrieves the public key for the team's WireGuard server interface."""
        plan = plan_for(team_k, wg_prefix=self.interface_prefix)
        iface = plan.wg_iface

        # Try fast in-process extraction from stored key
        key_path = self._get_key_file_path(iface)
        if key_path.exists():
            try:
                priv = key_path.read_text().strip()
                if priv:
                    return public_key_from_private(priv)
            except Exception:
                pass

        # Fallback to kernel interrogation via wg show
        res = await self.runner.run(wg.show_public_key(iface), check=True)
        return res.stdout.strip()

    async def list_peers(self, team_k: int) -> Dict[str, Dict[str, Any]]:
        """Parses active peers and statistics for team k from 'wg show <iface> dump'.

        Returns:
            Dictionary mapping peer public key to peer statistics (endpoint, allowed_ips, rx, tx, etc.).
        """
        plan = plan_for(team_k, wg_prefix=self.interface_prefix)
        iface = plan.wg_iface
        res = await self.runner.run(wg.show_dump(iface), check=False)
        if res.returncode != 0:
            return {}

        peers: Dict[str, Dict[str, Any]] = {}
        lines = res.stdout.strip().splitlines()
        # First line is interface details, following lines are peers
        for line in lines[1:]:
            parts = line.strip().split("\t")
            if len(parts) >= 8:
                pub, psk, endpoint, allowed_ips, latest_handshake, rx_bytes, tx_bytes, keepalive = parts[:8]
                peers[pub] = {
                    "public_key": pub,
                    "preshared_key": psk,
                    "endpoint": endpoint,
                    "allowed_ips": allowed_ips,
                    "latest_handshake": int(latest_handshake) if latest_handshake.isdigit() else 0,
                    "transfer_rx": int(rx_bytes) if rx_bytes.isdigit() else 0,
                    "transfer_tx": int(tx_bytes) if tx_bytes.isdigit() else 0,
                    "persistent_keepalive": keepalive,
                }
        return peers

    async def generate_member_config(
        self,
        team_k: int,
        member_name: str,
        client_ip: str,
        server_endpoint: Optional[str] = None,
        public_key: Optional[str] = None,
        preshared_key: Optional[str] = None,
        private_key: Optional[str] = None,
    ) -> MemberCredentials:
        """Generates keypair, preshared key, and client wireguard .conf file content.

        Args:
            team_k: Team identifier index k.
            member_name: Descriptive name or handle for the member.
            client_ip: Allocated IPv4 address for the member within 10.k.0.0/24.
            server_endpoint: Public IP/domain of the host server (default: settings.wg_public_endpoint).
            public_key: Optional pre-existing client public key (if client generates their own).
            preshared_key: Optional existing preshared key.
            private_key: Optional existing client private key.

        Returns:
            MemberCredentials populated with keys and renderable client configuration text.
        """
        plan = plan_for(team_k, wg_prefix=self.interface_prefix, base_port=self.settings.wg_base_port)

        client_priv: Optional[str] = None
        client_pub: str

        # TODO: this does not make sense
        if private_key:
            client_priv = private_key
            client_pub = public_key if public_key else public_key_from_private(private_key)
        elif public_key:
            client_pub = public_key
        else:
            client_priv, client_pub = generate_keypair()


        psk = preshared_key if preshared_key else generate_preshared_key()
        server_pub = await self.get_server_public_key(team_k)

        endpoint_host = server_endpoint or self.settings.wg_public_endpoint
        endpoint_str = f"{endpoint_host}:{plan.listen_port}"

        # Clean client_ip to ensure /32 mask for [Interface]
        clean_ip = client_ip.split("/")[0]

        # AllowedIPs specifies both the VPN subnet and the team production container subnet (split-tunnel)
        allowed_ips = f"{plan.vpn_subnet}, {plan.lan_subnet}"

        config_text = render_wireguard_client_conf(
            client_ip=clean_ip,
            server_pub=server_pub,
            endpoint=endpoint_str,
            allowed_ips=allowed_ips,
            keepalive=self.settings.wg_keepalive,
            client_priv=client_priv,
            psk=psk,
        )

        return MemberCredentials(
            name=member_name,
            client_ip=clean_ip,
            public_key=client_pub,
            private_key=client_priv,
            preshared_key=psk,
            config_text=config_text,
            server_endpoint=endpoint_str,
            server_public_key=server_pub,
        )
