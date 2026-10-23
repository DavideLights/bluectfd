#!/usr/bin/env bash
# ==============================================================================
# BlueCTFd / BlueAgent - Production Host Infrastructure Bootstrap
# Target OS: Debian 13 (Trixie) x86_64
#
# Configures kernel IP forwarding, Open vSwitch + OVN control plane, ZFS storage
# pool (blueagent-zfs), Incus daemon + underlay bridge (incusbr0), Distrobuilder
# runtime directories, and Docker forwarding compatibility rules.
#
# Usage:
#   sudo ./scripts/bootstrap_host.sh           # Run full idempotent bootstrap
#   sudo ./scripts/bootstrap_host.sh --check   # Verify host readiness without changes
# ==============================================================================
set -euo pipefail

INCUS_POOL="${BLUEAGENT_ZFS_POOL:-blueagent-zfs}"
INCUS_BRIDGE="${BLUEAGENT_UPLINK_BRIDGE:-incusbr0}"
OVN_NB_CONN="tcp:127.0.0.1:6641"
BUILD_DIR="${BLUEAGENT_IMAGE_BUILD_DIR:-/var/lib/blueagent/builds}"
LOCK_DIR="${BLUEAGENT_LOCK_DIRECTORY:-/tmp/blueagent_locks}"
WG_DIR="${BLUEAGENT_WIREGUARD_CONFIG_DIR:-/etc/wireguard}"

log_info()  { printf "\033[1;34m[*]\033[0m %s\n" "$*"; }
log_ok()    { printf "\033[1;32m[✔]\033[0m %s\n" "$*"; }
log_warn()  { printf "\033[1;33m[!]\033[0m %s\n" "$*"; }
log_err()   { printf "\033[1;31m[✘]\033[0m %s\n" "$*" >&2; }

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        log_err "This script must be run as root (e.g., sudo $0)."
        exit 1
    fi
}

check_host() {
    local failures=0
    log_info "Checking BlueAgent host readiness..."

    for cmd in incus ovs-vsctl ovn-nbctl wg nft ip distrobuilder debootstrap python3; do
        if command -v "${cmd}" &>/dev/null; then
            log_ok "Binary present: ${cmd} ($(command -v "${cmd}"))"
        else
            log_err "Missing binary: ${cmd}"
            failures=$((failures + 1))
        fi
    done

    if [[ "$(sysctl -n net.ipv4.ip_forward 2>/dev/null || echo 0)" == "1" ]]; then
        log_ok "Kernel IPv4 forwarding enabled (net.ipv4.ip_forward=1)"
    else
        log_err "Kernel IPv4 forwarding is disabled"
        failures=$((failures + 1))
    fi

    if incus storage show "${INCUS_POOL}" &>/dev/null; then
        log_ok "Incus ZFS storage pool ready: ${INCUS_POOL}"
    else
        log_err "Incus storage pool missing: ${INCUS_POOL}"
        failures=$((failures + 1))
    fi

    if incus network show "${INCUS_BRIDGE}" &>/dev/null; then
        log_ok "Incus underlay bridge ready: ${INCUS_BRIDGE}"
    else
        log_err "Incus underlay bridge missing: ${INCUS_BRIDGE}"
        failures=$((failures + 1))
    fi

    if ovn-nbctl --timeout=3 show &>/dev/null; then
        log_ok "OVN Northbound database reachable"
    else
        log_err "OVN Northbound database unreachable"
        failures=$((failures + 1))
    fi

    if [[ "${failures}" -eq 0 ]]; then
        log_ok "All host prerequisite checks passed."
        return 0
    else
        log_err "${failures} check(s) failed. Run: sudo $0"
        return 1
    fi
}

if [[ "${1:-}" == "--check" ]]; then
    require_root
    check_host
    exit $?
fi

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    sed -n "2,13p" "$0" | sed "s/^# \{0,1\}//"
    exit 0
fi

require_root

log_info "Starting BlueCTFd / BlueAgent production host bootstrap..."

# ------------------------------------------------------------------------------
# 1. System Packages & ZFS Kernel Support
# ------------------------------------------------------------------------------
log_info "[1/6] Updating APT cache and installing system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y \
    openvswitch-switch \
    ovn-central \
    ovn-host \
    incus \
    wireguard \
    wireguard-tools \
    iproute2 \
    nftables \
    "linux-headers-$(uname -r)" \
    distrobuilder \
    debootstrap \
    squashfs-tools \
    gpg \
    curl \
    git \
    jq \
    python3 \
    python3-venv \
    python3-pip || apt-get install -y \
    openvswitch-switch ovn-central ovn-host incus wireguard wireguard-tools \
    iproute2 nftables linux-headers-amd64 distrobuilder debootstrap \
    squashfs-tools gpg curl git jq python3 python3-venv python3-pip

if ! dpkg -s zfsutils-linux &>/dev/null; then
    log_info "Installing zfsutils-linux (requires contrib repository enabled)..."
    apt-get install -y -t stable-backports zfsutils-linux 2>/dev/null \
        || apt-get install -y zfsutils-linux
fi

modprobe wireguard || log_warn "Could not modprobe wireguard (may be built into kernel)"
modprobe zfs || log_warn "Could not modprobe zfs — verify DKMS build against running kernel headers"

# ------------------------------------------------------------------------------
# 2. Kernel IPv4 & IPv6 Forwarding
# ------------------------------------------------------------------------------
log_info "[2/6] Persisting kernel IP forwarding in /etc/sysctl.d/99-blueagent.conf..."
cat << 'SYSCTL' > /etc/sysctl.d/99-blueagent.conf
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
SYSCTL
sysctl --system >/dev/null

# ------------------------------------------------------------------------------
# 3. Open vSwitch (OVS) & OVN Control Plane
# ------------------------------------------------------------------------------
log_info "[3/6] Enabling and configuring Open vSwitch + OVN control plane..."
systemctl enable --now openvswitch-switch ovn-central ovn-host

ovs-vsctl set open_vswitch . \
    external_ids:ovn-remote=unix:/run/ovn/ovnsb_db.sock \
    external_ids:ovn-encap-type=geneve \
    external_ids:ovn-encap-ip=127.0.0.1

ovn-nbctl set-connection ptcp:6641:127.0.0.1

# ------------------------------------------------------------------------------
# 4. Incus Initialization, ZFS CoW Storage Pool & Underlay Network (incusbr0)
# ------------------------------------------------------------------------------
log_info "[4/6] Initializing Incus, ZFS pool (${INCUS_POOL}), and OVN uplink (${INCUS_BRIDGE})..."
systemctl enable --now incus.socket incus.service || true

incus admin init --auto

incus config set network.ovn.northbound_connection "${OVN_NB_CONN}"

if ! incus storage show "${INCUS_POOL}" &>/dev/null; then
    incus storage create "${INCUS_POOL}" zfs
fi

if incus profile device show default | grep -q "^root:"; then
    incus profile device set default root pool="${INCUS_POOL}" || true
else
    incus profile device add default root disk path=/ pool="${INCUS_POOL}"
fi

if ! incus network show "${INCUS_BRIDGE}" &>/dev/null; then
    incus network create "${INCUS_BRIDGE}" \
        ipv4.address=172.16.0.1/24 \
        ipv4.nat=true \
        ipv6.address=none
fi

incus network set "${INCUS_BRIDGE}" \
    ipv4.address=172.16.0.1/24 \
    ipv4.nat=true \
    ipv4.dhcp.ranges=172.16.0.2-172.16.0.99 \
    ipv4.ovn.ranges=172.16.0.100-172.16.0.199

# ------------------------------------------------------------------------------
# 5. Runtime Directories & Docker Forwarding Compatibility
# ------------------------------------------------------------------------------
log_info "[5/6] Preparing BlueAgent runtime directories and firewall compatibility..."
mkdir -p "${BUILD_DIR}" "${WG_DIR}" "${LOCK_DIR}"
chmod 700 "${WG_DIR}"
chmod 755 "${BUILD_DIR}" "${LOCK_DIR}"

if command -v iptables &>/dev/null && iptables -L DOCKER-USER &>/dev/null; then
    log_info "Docker detected: ensuring DOCKER-USER allows WireGuard <-> br-team-k forwarding..."
    iptables -C DOCKER-USER -j ACCEPT 2>/dev/null || iptables -I DOCKER-USER -j ACCEPT
fi

# ------------------------------------------------------------------------------
# 6. Post-Bootstrap Verification
# ------------------------------------------------------------------------------
log_info "[6/6] Running post-bootstrap verification..."
check_host
log_ok "Host bootstrap complete! Next steps: set up Python venvs or Docker Compose (see README.md)."
