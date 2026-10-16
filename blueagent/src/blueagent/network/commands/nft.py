"""Command builders for nftables."""

def apply_batch() -> list[str]:
    return ["nft", "-f", "-"]

def add_forward_pair(table: str, iface_a: str, iface_b: str) -> list[str]:
    return [
        "nft",
        "add",
        "element",
        "inet",
        table,
        "team_links",
        f'{{ "{iface_a}" . "{iface_b}", "{iface_b}" . "{iface_a}" }}',
    ]

def del_forward_pair(table: str, iface_a: str, iface_b: str) -> list[str]:
    return [
        "nft",
        "delete",
        "element",
        "inet",
        table,
        "team_links",
        f'{{ "{iface_a}" . "{iface_b}", "{iface_b}" . "{iface_a}" }}',
    ]
