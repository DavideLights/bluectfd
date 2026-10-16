"""Command builders for WireGuard (wg)."""

def set_listen_port_and_key(dev: str, port: int, key_via_stdin: bool = True) -> list[str]:
    cmd = ["wg", "set", dev, "listen-port", str(port)]
    if key_via_stdin:
        cmd.extend(["private-key", "/dev/stdin"])
    return cmd

def set_peer(
    dev: str,
    pubkey: str,
    allowed_ips: str,
    endpoint: str | None = None,
    psk_via_stdin: bool = False,
) -> list[str]:
    cmd = ["wg", "set", dev, "peer", pubkey, "allowed-ips", allowed_ips]
    if endpoint:
        cmd.extend(["endpoint", endpoint])
    if psk_via_stdin:
        cmd.extend(["preshared-key", "/dev/stdin"])
    return cmd

def remove_peer(dev: str, pubkey: str) -> list[str]:
    return ["wg", "set", dev, "peer", pubkey, "remove"]

def show_public_key(dev: str) -> list[str]:
    return ["wg", "show", dev, "public-key"]

def show_dump(dev: str) -> list[str]:
    return ["wg", "show", dev, "dump"]

