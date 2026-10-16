"""Template renderers for network configuration files."""

def render_networkd_conf(ip: str, gateway: str, dns: str = "1.1.1.1", dev: str = "eth0") -> str:
    """Renders a systemd-networkd .network configuration file."""
    return (
        f"[Match]\n"
        f"Name={dev}\n\n"
        f"[Network]\n"
        f"Address={ip}/24\n"
        f"Gateway={gateway}\n"
        f"DNS={dns}\n"
    )

def render_interfaces_conf(ip: str, gateway: str, dev: str = "eth0") -> str:
    """Renders a Debian/Alpine /etc/network/interfaces configuration block."""
    return (
        f"auto lo\n"
        f"iface lo inet loopback\n\n"
        f"auto {dev}\n"
        f"iface {dev} inet static\n"
        f"    address {ip}/24\n"
        f"    gateway {gateway}\n"
    )

def render_nftables_base(table: str) -> str:
    """Renders the base nftables ruleset for the control plane."""
    return f"""
add table inet {table}
add set inet {table} team_links {{ type ifname . ifname; flags interval; }}

add chain inet {table} forward {{ type filter hook forward priority filter; policy accept; }}
flush chain inet {table} forward
add rule inet {table} forward tcp flags syn / syn,rst tcp option maxseg size set rt mtu
add rule inet {table} forward iifname . oifname @team_links accept
add rule inet {table} forward iifname "wg-*" oifname "br-team-*" drop
add rule inet {table} forward iifname "br-team-*" oifname "wg-*" drop
add rule inet {table} forward iifname "br-team-*" oifname "br-team-*" drop
add rule inet {table} forward iifname "wg-*" oifname "wg-*" drop
add rule inet {table} forward iifname "br-team-*" accept
add rule inet {table} forward oifname "br-team-*" ct state established,related accept

add chain inet {table} postrouting {{ type nat hook postrouting priority srcnat; policy accept; }}
flush chain inet {table} postrouting
add rule inet {table} postrouting ip saddr 10.0.0.0/8 ip daddr != 10.0.0.0/8 masquerade
""".strip()


def render_wireguard_client_conf(
    client_ip: str,
    server_pub: str,
    endpoint: str,
    allowed_ips: str,
    keepalive: int = 25,
    client_priv: str | None = None,
    psk: str | None = None,
) -> str:
    """Renders a client WireGuard .conf file."""
    clean_ip = client_ip.split("/")[0]
    psk_line = f"PresharedKey = {psk}\n" if psk else ""
    priv_line = f"PrivateKey = {client_priv}\n" if client_priv else "# PrivateKey generated client-side\n"

    return f"""[Interface]
{priv_line}Address = {clean_ip}/32

[Peer]
PublicKey = {server_pub}
{psk_line}Endpoint = {endpoint}
AllowedIPs = {allowed_ips}
PersistentKeepalive = {keepalive}
""".strip()


