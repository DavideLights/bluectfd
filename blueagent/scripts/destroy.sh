#!/usr/bin/env bash
# ==============================================================================
# BlueAgent v2 - Host Infrastructure Teardown Script
# Target OS: Debian 13 (Trixie)
# Matches exactly inverse-commands.sh, destroying resources in reverse LIFO order.
# ==============================================================================
set -uo pipefail

echo "[*] Tearing down BlueAgent v2 Host Artifacts on Debian 13..."

VERIFY_UUID=${VERIFY_UUID:-0}

# ------------------------------------------------------------------------------
# 1. Stop and Delete Containers
# ------------------------------------------------------------------------------
echo "[1/7] Stopping and deleting containers..."

# 1.1 Cloned verification container
if sudo incus info "c0-web-v-${VERIFY_UUID}" &>/dev/null; then
    echo "  - Stopping c0-web-v-${VERIFY_UUID}..."
    sudo incus stop "c0-web-v-${VERIFY_UUID}" --force 2>/dev/null || true
    echo "  - Deleting c0-web-v-${VERIFY_UUID}..."
    sudo incus delete "c0-web-v-${VERIFY_UUID}" --force 2>/dev/null || true
fi

# 1.2 Production container
if sudo incus info c0-web &>/dev/null; then
    echo "  - Stopping c0-web..."
    sudo incus stop c0-web --force 2>/dev/null || true
    echo "  - Deleting c0-web..."
    sudo incus delete c0-web --force 2>/dev/null || true
fi

# 1.3 Cleanup lingering verification clones
for inst in $(sudo incus list -c n --format csv 2>/dev/null | grep -E '^c0-web-v-' || true); do
    echo "  - Deleting lingering clone $inst..."
    sudo incus stop "$inst" --force 2>/dev/null || true
    sudo incus delete "$inst" --force 2>/dev/null || true
done

# ------------------------------------------------------------------------------
# 2. Delete OVN Networks
# ------------------------------------------------------------------------------
echo "[2/7] Deleting OVN networks..."

if sudo incus network show "verify-ovn-${VERIFY_UUID}" &>/dev/null; then
    echo "  - Deleting OVN network verify-ovn-${VERIFY_UUID}..."
    sudo incus network delete "verify-ovn-${VERIFY_UUID}" 2>/dev/null || true
fi

for ovn_net in $(sudo incus network list --format csv -c n,t 2>/dev/null | grep ',ovn' | awk -F',' '{print $1}' | grep -E '^verify-ovn-' || true); do
    echo "  - Deleting lingering OVN network $ovn_net..."
    sudo incus network delete "$ovn_net" 2>/dev/null || true
done

# ------------------------------------------------------------------------------
# 3. Delete Incus Profiles
# ------------------------------------------------------------------------------
echo "[3/7] Deleting Incus profiles..."

if sudo incus profile show team-0-profile &>/dev/null; then
    echo "  - Deleting profile team-0-profile..."
    sudo incus profile delete team-0-profile 2>/dev/null || true
fi

# ------------------------------------------------------------------------------
# 4. Flush and Remove nftables Firewall Table
# ------------------------------------------------------------------------------
echo "[4/7] Freeing nftables tables and rules..."

if command -v nft &>/dev/null; then
    echo "  - Flushing and deleting table inet blueagent..."
    sudo nft flush table inet blueagent 2>/dev/null || true
    sudo nft delete table inet blueagent 2>/dev/null || true
fi

# ------------------------------------------------------------------------------
# 5. Teardown Network Interfaces (Linux Bridges & WireGuard)
# ------------------------------------------------------------------------------
echo "[5/7] Tearing down network interfaces..."

# 5.1 WireGuard interface
if ip link show dev wg-0 &>/dev/null; then
    echo "  - Removing WireGuard interface wg-0..."
    sudo ip link set dev wg-0 down 2>/dev/null || true
    sudo ip link delete dev wg-0 2>/dev/null || true
fi

# 5.2 Linux Bridge interface
if ip link show dev br-team-0 &>/dev/null; then
    echo "  - Removing Linux bridge br-team-0..."
    sudo ip link set dev br-team-0 down 2>/dev/null || true
    sudo ip link delete dev br-team-0 type bridge 2>/dev/null || true
fi

# ------------------------------------------------------------------------------
# 6. Remove Keys and Temporary Files
# ------------------------------------------------------------------------------
echo "[6/7] Removing cryptographic keys and temporary lock files..."

rm -f wg-0-private.key wg-0-public.key client-0-private.key client-0-public.key
rm -rf /tmp/blueagent_locks/*.lock 2>/dev/null || true
rm -f /tmp/10-eth0.network 2>/dev/null || true

# ------------------------------------------------------------------------------
# 7. Free Hypervisor Storage and OVN/OVS Integrations
# ------------------------------------------------------------------------------
echo "[7/7] Freeing Incus storage pool and OVN/OVS settings..."

# 7.1 Delete ZFS storage pool in Incus
if sudo incus storage show blueagent-zfs &>/dev/null; then
    echo "  - Deleting Incus storage pool blueagent-zfs..."
    sudo incus storage delete blueagent-zfs 2>/dev/null || true
fi

# 7.2 Unset OVN connection in Incus
sudo incus config unset network.ovn.northbound_connection 2>/dev/null || true

# 7.3 Revert OVN IP ranges on incusbr0
if sudo incus network show incusbr0 &>/dev/null; then
    echo "  - Reverting OVN IP ranges on incusbr0..."
    sudo incus network unset incusbr0 ipv4.ovn.ranges 2>/dev/null || true
fi

# 7.4 Remove OVN Northbound passive TCP connection
if command -v ovn-nbctl &>/dev/null; then
    echo "  - Removing OVN Northbound listener on 6641..."
    sudo ovn-nbctl del-connection 2>/dev/null || true
fi

# 7.5 Reset Open vSwitch external_ids
if command -v ovs-vsctl &>/dev/null; then
    echo "  - Resetting Open vSwitch external_ids..."
    sudo ovs-vsctl remove open_vswitch . external_ids ovn-remote 2>/dev/null || true
    sudo ovs-vsctl remove open_vswitch . external_ids ovn-encap-type 2>/dev/null || true
    sudo ovs-vsctl remove open_vswitch . external_ids ovn-encap-ip 2>/dev/null || true
fi

echo "[✔] BlueAgent v2 Teardown Complete."
