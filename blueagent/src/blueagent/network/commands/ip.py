"""Command builders for the iproute2 suite."""

def link_show(dev: str, json: bool = False) -> list[str]:
    cmd = ["ip"]
    if json:
        cmd.append("-json")
    cmd.extend(["link", "show", "dev", dev])
    return cmd

def link_add_bridge(name: str) -> list[str]:
    return ["ip", "link", "add", "name", name, "type", "bridge"]

def link_add_wireguard(dev: str) -> list[str]:
    return ["ip", "link", "add", "dev", dev, "type", "wireguard"]

def addr_replace(cidr: str, dev: str) -> list[str]:
    return ["ip", "addr", "replace", cidr, "dev", dev]

def route_replace_default(via: str, dev: str) -> list[str]:
    return ["ip", "route", "replace", "default", "via", via, "dev", dev]

def link_up(dev: str) -> list[str]:
    return ["ip", "link", "set", "dev", dev, "up"]

def link_down(dev: str) -> list[str]:
    return ["ip", "link", "set", "dev", dev, "down"]

def link_set_mtu(dev: str, mtu: int) -> list[str]:
    return ["ip", "link", "set", "dev", dev, "mtu", str(mtu)]

def link_delete(dev: str, dev_type: str | None = None) -> list[str]:
    cmd = ["ip", "link", "delete", "dev", dev]
    if dev_type:
        cmd.extend(["type", dev_type])
    return cmd

def netns_exec(ns: str, command: list[str]) -> list[str]:
    return ["ip", "netns", "exec", ns] + command
