"""Device dictionary builders for Incus API."""

def nic_bridge(bridge_name: str, dev_name: str = "eth0", nictype: str = "bridged") -> dict:
    """Builds a nic device mapping to a Linux host bridge."""
    return {
        dev_name: {
            "type": "nic",
            "nictype": nictype,
            "parent": bridge_name,
            "name": dev_name,
        }
    }


def nic_ovn(network_name: str, dev_name: str = "eth0") -> dict:
    """Builds a nic device mapping to an Incus OVN network."""
    return {
        dev_name: {
            "type": "nic",
            "network": network_name,
            "name": dev_name,
        }
    }
