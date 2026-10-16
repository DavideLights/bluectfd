#!/usr/bin/env bash
# ==============================================================================
# BlueAgent v2 - Host Infrastructure Bootstrap Script
# Target OS: Debian 13 (Trixie)
# Matches exactly the verified commands in commands.sh
# ==============================================================================
set -euo pipefail

echo "[*] Initializing BlueAgent v2 Host Environment on Debian 13..."

# ------------------------------------------------------------------------------
# 1. Package Installation and Kernel Modules
# ------------------------------------------------------------------------------
echo "[1/8] Updating package lists and installing required dependencies..."
sudo apt-get update
sudo apt-get install -y \
    openvswitch-switch \
    ovn-central \
    ovn-host \
    incus \
    wireguard \
    iproute2 \
    nftables \
    linux-headers-amd64

# Install ZFS utilities (supports backports or standard trixie repo)
if ! dpkg -s zfsutils-linux &>/dev/null; then
    echo "[+] Installing zfsutils-linux..."
    sudo apt-get install -y -t stable-backports zfsutils-linux 2>/dev/null || sudo apt-get install -y zfsutils-linux
fi

# Enable Kernel IP Forwarding (IPv4 & IPv6)
echo "[+] Enabling Kernel IP Forwarding..."
sudo sysctl -w net.ipv4.ip_forward=1
sudo sysctl -w net.ipv6.conf.all.forwarding=1
cat << 'EOF' | sudo tee /etc/sysctl.d/99-blueagent.conf > /dev/null
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
EOF

# ------------------------------------------------------------------------------
# 2. Open vSwitch (OVS) and OVN Control Plane Integration
# ------------------------------------------------------------------------------
echo "[2/8] Configuring Open vSwitch and OVN Northbound/Southbound databases..."
sudo ovs-vsctl set open_vswitch . \
    external_ids:ovn-remote=unix:/run/ovn/ovnsb_db.sock \
    external_ids:ovn-encap-type=geneve \
    external_ids:ovn-encap-ip=127.0.0.1

sudo ovn-nbctl set-connection ptcp:6641:127.0.0.1
sudo incus config set network.ovn.northbound_connection tcp:127.0.0.1:6641

# ------------------------------------------------------------------------------
# 3. Incus Initialization, ZFS Pool, and Underlay Bridge (incusbr0)
# ------------------------------------------------------------------------------
echo "[3/8] Initializing Incus and configuring storage pool..."
if ! sudo incus storage show blueagent-zfs &>/dev/null; then
    sudo incus storage create blueagent-zfs zfs
fi

sudo incus admin init --auto

echo "[+] Setting up incusbr0 with DHCP and OVN IP pools..."
sudo incus network set incusbr0 ipv4.address=172.16.0.1/24 ipv4.nat=true
sudo incus network set incusbr0 ipv4.dhcp.ranges=172.16.0.2-172.16.0.99 ipv4.ovn.ranges=172.16.0.100-172.16.0.199

# ------------------------------------------------------------------------------
# 4. Team 0 Production L2 Bridge
# ------------------------------------------------------------------------------
echo "[4/8] Creating Team 0 production L2 bridge (br-team-0)..."
if ! ip link show dev br-team-0 &>/dev/null; then
    sudo ip link add name br-team-0 type bridge
    sudo ip addr add 10.0.1.1/24 dev br-team-0
    sudo ip link set dev br-team-0 up
fi

# ------------------------------------------------------------------------------
# 5. WireGuard L3 Point-to-Point Tunnel (wg-0)
# ------------------------------------------------------------------------------
echo "[5/8] Generating keys and establishing WireGuard interface (wg-0)..."
wg genkey | tee wg-0-private.key | wg pubkey > wg-0-public.key
wg genkey | tee client-0-private.key | wg pubkey > client-0-public.key

if ! ip link show dev wg-0 &>/dev/null; then
    sudo ip link add dev wg-0 type wireguard
    sudo ip addr add 10.0.0.1/30 dev wg-0
    sudo wg set wg-0 listen-port 51820 private-key wg-0-private.key
    sudo wg set wg-0 peer "$(cat client-0-public.key)" allowed-ips 10.0.0.2/32
    sudo ip link set dev wg-0 up
fi

# ------------------------------------------------------------------------------
# 6. nftables Firewall: Forwarding, TCP MSS Clamping & NAT Masquerade
# ------------------------------------------------------------------------------
echo "[6/8] Configuring nftables filtering and traffic engineering..."
sudo nft flush table inet blueagent 2>/dev/null || true
sudo nft add table inet blueagent
sudo nft add chain inet blueagent forward '{ type filter hook forward priority filter; policy accept; }'
sudo nft add rule inet blueagent forward tcp flags syn / syn,rst tcp option maxseg size set rt mtu
sudo nft add rule inet blueagent forward iifname "br-team-0" accept
sudo nft add rule inet blueagent forward oifname "br-team-0" accept
sudo nft add rule inet blueagent forward iifname "wg-0" accept
sudo nft add rule inet blueagent forward oifname "wg-0" accept
sudo nft add chain inet blueagent postrouting '{ type nat hook postrouting priority srcnat; policy accept; }'
sudo nft add rule inet blueagent postrouting ip saddr 10.0.1.0/24 ip daddr != 10.0.1.0/24 masquerade
sudo nft add rule inet blueagent postrouting ip saddr 10.0.0.0/30 ip daddr != 10.0.0.0/30 masquerade

# ------------------------------------------------------------------------------
# 7. Team 0 Profile, Container Launch & systemd-networkd Static IP
# ------------------------------------------------------------------------------
echo "[7/8] Launching production container (c0-web) and configuring networking..."
sudo incus profile create team-0-profile 2>/dev/null || true
sudo incus profile device add team-0-profile eth0 nic nictype=bridged parent=br-team-0 name=eth0 2>/dev/null || true

if ! sudo incus info c0-web &>/dev/null; then
    sudo incus launch ubuntu:22.04 c0-web --profile default --profile team-0-profile --storage blueagent-zfs
fi

# Inject static IP configuration via systemd-networkd (independent of netplan/cloud-init)
sudo incus exec c0-web -- bash -c 'cat << "EOF" > /etc/systemd/network/10-eth0.network
[Match]
Name=eth0
[Network]
Address=10.0.1.2/24
Gateway=10.0.1.1
DNS=1.1.1.1
DNS=8.8.8.8
EOF
systemctl restart systemd-networkd'

# ------------------------------------------------------------------------------
# 8. OVN Verification Sandbox Instantiation
# ------------------------------------------------------------------------------
echo "[8/8] Provisioning OVN verification sandbox..."
VERIFY_UUID=${VERIFY_UUID:-0}

if ! sudo incus network show "verify-ovn-${VERIFY_UUID}" &>/dev/null; then
    sudo incus network create "verify-ovn-${VERIFY_UUID}" --type=ovn network=incusbr0 ipv4.address=10.0.1.1/24 ipv4.nat=true
fi

if ! sudo incus info "c0-web-v-${VERIFY_UUID}" &>/dev/null; then
    sudo incus copy c0-web "c0-web-v-${VERIFY_UUID}" --instance-only
    sudo incus config device override "c0-web-v-${VERIFY_UUID}" eth0 network="verify-ovn-${VERIFY_UUID}" parent=
    sudo incus start "c0-web-v-${VERIFY_UUID}"
fi

echo "[✔] BlueAgent v2 Host Environment Setup Complete!"
