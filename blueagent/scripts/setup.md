# OVN and Incus Setup Guide

Complete and updated operational guide for initializing the **OVN + Incus** stack and instantiating the **Team 0 Verification Sandbox**.  
This version integrates **all practical fixes identified during testing on Debian 13**:

1. **OVN/OVS integration configuration** (`ovs-vsctl set open_vswitch . external_ids:ovn-remote=...`).
2. **Mandatory DHCP range definition** (`ipv4.dhcp.ranges`) alongside the OVN range (`ipv4.ovn.ranges`) on `incusbr0`.
3. **Clearing the inherited parameter** **`parent=`** during the `incus config device override` command.
4. **Consistent usage of the attribute** **`network=`** for all OVN-managed networks.

---

### 🚀 Definitive Operational Guide: OVN + Incus Setup & Team 0 Sandbox

---

#### 📐 Structure of Command Explanations
Every command is illustrated according to the following fixed structure:
> * **CLI Command:** *(Exact command to execute in terminal)*
> * **Operational Purpose:** *(Concrete effect of the command on the infrastructure)*
> * **Theoretical Keywords and Explanation:**
>   * **[Concept 1]**: *(Explanation of computer science or networking principle)*
>   * **[Concept 2]**: *(Explanation of Linux kernel or OVN mechanism)*

---

#### PHASE 0: Prerequisites, IP Forwarding, and OVN/OVS Binding

##### Step 0.1: Installation of Network Packages and System Tools
* **CLI Command:**
```bash
sudo apt-get update && sudo apt-get install -y     openvswitch-switch ovn-central ovn-host incus zfsutils-linux wireguard iproute2 nftables
```
* **Operational Purpose:** Installs the Open vSwitch datapath, OVN control plane, Incus hypervisor, ZFS support, and network tools on the Debian 13 host server.
* **Theoretical Keywords and Explanation:**
    * **Open vSwitch (OVS)**: Multilayer software network switch designed to automate network switching in virtualized environments.
    * **OVN (Open Virtual Network)**: Software-Defined Networking (SDN) system developed by the OVS project to provide encapsulated L2/L3 logical networks and virtual routers.

---

##### Step 0.2: Enabling Kernel IP Forwarding
* **CLI Command:**
```bash
sudo sysctl -w net.ipv4.ip_forward=1
```
* **Operational Purpose:** Enables the host kernel's capability to route packets between different network interfaces.
* **Theoretical Keywords and Explanation:**
    * **IP Forwarding**: Linux kernel flag that converts the host from a simple end-system to an L3 router.
    * **sysctl**: Utility for real-time configuration of kernel parameters resident in the `/proc/sys/` hierarchy.

---

##### Step 0.3: OVS External IDs Configuration for OVN Host
* **CLI Command:**
```bash
sudo ovs-vsctl set open_vswitch .    external_ids:ovn-remote=unix:/run/ovn/ovnsb_db.sock    external_ids:ovn-encap-type=geneve    external_ids:ovn-encap-ip=127.0.0.1
```
* **Operational Purpose:** Connects the local `ovn-controller` daemon to the OVN Southbound Database and instructs Open vSwitch to use Geneve encapsulation on the loopback interface.
* **Theoretical Keywords and Explanation:**
    * **Geneve (Generic Network Virtualization Encapsulation)**: L3/L4 overlay tunneling protocol encapsulating L2 Ethernet frames inside UDP packets.
    * **OVN Southbound DB**: OVN Database containing physical network state and flow tables distributed to each chassis/host.

---

#### PHASE 1: Incus Initialization, OVN Connection, and Uplink (`incusbr0`)

##### Step 1.1: Automatic Incus Initialization and ZFS Storage Pool
* **CLI Command:**
```bash
sudo incus admin init --auto --storage-backend=zfs --storage-pool=blueagent-zfs
```
* **Operational Purpose:** Non-interactively initializes Incus allocating a ZFS-based Copy-on-Write (CoW) storage pool (`blueagent-zfs`).
* **Theoretical Keywords and Explanation:**
    * **Copy-on-Write (CoW)**: Storage allocation mechanism enabling filesystem duplication in ~100 ms via block-level differential snapshots.
    * **Non-Interactive Init (`--auto`)**: Mandatory flag in Incus to confirm CLI execution parameters without user prompts.

---

##### Step 1.2: OVN TCP Socket Configuration for Incus
* **CLI Command:**
```bash
sudo ovn-nbctl set-connection ptcp:6641:127.0.0.1
sudo incus admin sql global "INSERT OR REPLACE INTO config (key, value) VALUES ('network.ovn.northbound_connection', 'tcp:127.0.0.1:6641');"
sudo systemctl restart incus
```
* **Operational Purpose:** Enables listening on OVN Northbound Database TCP port 6641 and registers the address in the internal Incus SQLite database.
* **Theoretical Keywords and Explanation:**
    * **OVN Northbound DB**: Database representing the logical network topology (switches, routers, and ports) received from the orchestrator.
    * **Incus OVN Driver**: Internal module in Incus translating `--type=ovn` network requests into logical objects inside the Northbound DB.

---

##### Step 1.3: Parent Uplink Configuration (`incusbr0`) with DHCP and OVN Subnets
* **CLI Command:**
```bash
incus network set incusbr0 ipv4.address=172.16.0.1/24 ipv4.nat=true
sudo incus network set incusbr0 ipv4.dhcp.ranges=172.16.0.2-172.16.0.99 ipv4.ovn.ranges=172.16.0.100-172.16.0.199
```
* **Operational Purpose:** Assigns gateway address `172.16.0.1/24` to `incusbr0`, defines DHCP range for traditional instances, and establishes the `ipv4.ovn.ranges` pool for OVN router uplink IPs.
* **Theoretical Keywords and Explanation:**
    * **Uplink Parent Network**: Underlay physical network or managed bridge providing WAN transit and egress for the OVN logical router.
    * **OVN IP Allocation Range**: Reserved IP address range automatically assigned by Incus to OVN logical router uplink connection interfaces.

---

#### PHASE 2: Team 0 Live Production Network Setup (Native Kernel)

##### Step 2.1: Creation of `br-team-0` Bridge and `wg-0` WireGuard Interface
* **CLI Command:**
```bash
# 1. Team 0 Production L2 Bridge
sudo ip link add name br-team-0 type bridge
sudo ip addr add 10.0.1.1/24 dev br-team-0
sudo ip link set dev br-team-0 up
# 2. Key Generation and WireGuard setup
wg genkey | tee wg-0-private.key | wg pubkey > wg-0-public.key
wg genkey | tee client-0-private.key | wg pubkey > client-0-public.key
sudo ip link add dev wg-0 type wireguard
sudo ip addr add 10.0.0.1/30 dev wg-0
sudo wg set wg-0 listen-port 51820 private-key wg-0-private.key
sudo wg set wg-0 peer "$(cat client-0-public.key)" allowed-ips 10.0.0.2/32
sudo ip link set dev wg-0 up
```
* **Operational Purpose:** Activates Team 0 production L2 switch (`10.0.1.1/24`) and establishes WireGuard L3 VPN tunnel (`10.0.0.1/30`).
* **Theoretical Keywords and Explanation:**
    * **Linux Bridge**: Kernel software switch forwarding L2 Ethernet frames based on MAC addresses.
    * **Cryptokey Routing**: WireGuard mechanism mapping peer public key to authorized IP address (`AllowedIPs`).

---

##### Step 2.2: `nftables` Firewall with TCP MSS Clamping and NAT Masquerade
* **CLI Command:**
```bash
sudo nft flush table inet blueagent 2>/dev/null || true
sudo nft add table inet blueagent
sudo nft add chain inet blueagent forward '{ type filter hook forward priority filter; policy accept; }'
sudo nft add rule inet blueagent forward iifname "br-team-0" accept
sudo nft add rule inet blueagent forward oifname "br-team-0" accept
sudo nft add rule inet blueagent forward iifname "wg-0" accept
sudo nft add rule inet blueagent forward oifname "wg-0" accept
sudo nft add rule inet blueagent forward tcp flags syn / syn,rst tcp option maxseg size set rt mtu
sudo nft add chain inet blueagent postrouting '{ type nat hook postrouting priority srcnat; policy accept; }'
sudo nft add rule inet blueagent postrouting ip saddr 10.0.1.0/24 ip daddr != 10.0.1.0/24 masquerade
sudo nft add rule inet blueagent postrouting ip saddr 10.0.0.0/30 ip daddr != 10.0.0.0/30 masquerade
```
* **Operational Purpose:** Authorizes packet forwarding, applies outbound NAT for containers, and forces TCP MSS Clamping to eliminate Path MTU Blackholes.
* **Theoretical Keywords and Explanation:**
    * **TCP MSS Clamping**: Overwriting Maximum Segment Size field in TCP SYN packets to adjust it to egress WAN/VPN interface MTU.
    * **Source NAT / Masquerade**: Dynamic translation of private packet source IPs to host public IP.

---

##### Step 2.3: Incus Profile and Launch of Live Container `c0-web`
* **CLI Command:**
```bash
# 1. Production Profile
incus profile create team-0-profile 2>/dev/null || true
incus profile device add team-0-profile eth0 nic nictype=bridged parent=br-team-0 name=eth0 2>/dev/null || true
# 2. Netplan injection for static IP (10.0.1.2)
incus profile config set team-0-profile user.network-config="
version: 2
ethernets:
  eth0:
    dhcp4: no
    addresses:
      - 10.0.1.2/24
    routes:
      - to: default
        via: 10.0.1.1
    nameservers:
      addresses: [1.1.1.1, 8.8.8.8]
"
# 3. Launch live production instance from official Ubuntu remote
incus launch ubuntu:22.04 c0-web --profile default --profile team-0-profile
```
* **Operational Purpose:** Creates and starts production container `c0-web` injecting static IP `10.0.1.2/24` and gateway `10.0.1.1` via cloud-init.
* **Theoretical Keywords and Explanation:**
    * **Unmanaged Bridge IP Configuration**: Manual or declarative injection (Netplan) of network settings when no DHCP server is present on bridge.
    * **Incus System Container**: Lightweight OS-level virtualization instance sharing host kernel.

---

#### PHASE 3: OVN Verification Sandbox Instantiation

##### Step 3.1: Session Variable Definition
* **CLI Command:**
```bash
export VERIFY_UUID="test01"
```
* **Operational Purpose:** Sets unique identification suffix to isolate ephemeral test session.
* **Theoretical Keywords and Explanation:**
    * **Session UUID**: Unique alphanumeric identifier preventing naming collisions across test networks and instances.

---

##### Step 3.2: OVN Sandbox Network Creation with `network=` Attribute
* **CLI Command:**
```bash
incus network create verify-ovn-${VERIFY_UUID} --type=ovn network=incusbr0 ipv4.address=10.0.1.1/24 ipv4.nat=true
```
* **Operational Purpose:** Instructs OVN to instantiate an isolated logical router and switch with gateway IP `10.0.1.1/24`, connected to `incusbr0` underlay via `network=` attribute.
* **Theoretical Keywords and Explanation:**
    * **Attribute `network=`**: Native Incus OVN identifier specifying parent uplink network (replaces `parent=`).
    * **Logical Datapath**: OVN software abstraction simulating L2/L3 components in user/kernel-space without altering root host routing.

---

##### Step 3.3: Instant Copy-on-Write (CoW) Snapshot of Live Container
* **CLI Command:**
```bash
incus copy c0-web c0-web-v-${VERIFY_UUID} --instance-only
```
* **Operational Purpose:** Clones production container image and state in ~100 ms without interrupting live service.
* **Theoretical Keywords and Explanation:**
    * **Snapshot CoW**: Instant duplication sharing immutable storage blocks with source, allocating space only for modified blocks.

---

##### Step 3.4: Device Override Clearing Inherited `parent=` Parameter
* **CLI Command:**
```bash
incus config device override c0-web-v-${VERIFY_UUID} eth0 network=verify-ovn-${VERIFY_UUID} parent=
```
* **Operational Purpose:** Removes association to native bridge `br-team-0` by unsetting `parent=` key and rebinds clone `eth0` NIC to OVN managed network.
* **Theoretical Keywords and Explanation:**
    * **Device Override**: Operation extracting device definition from profile into instance-specific local configuration.
    * **Single eth0 Topological Fidelity**: Maintaining single `eth0` interface and original IP inside sandbox ensuring zero false negatives and zero side-channel leaks.

---

##### Step 3.5: Launch Cloned Container in OVN Sandbox
* **CLI Command:**
```bash
incus start c0-web-v-${VERIFY_UUID}
```
* **Operational Purpose:** Starts clone inside OVN virtual network environment.
* **Theoretical Keywords and Explanation:**
    * **Dark Sandbox**: Isolated broadcast domain where exploit tests run in total separation from game environment.

---

#### PHASE 4: Empirical Testing and Verification

##### Step 4.1: Addressing, Routing, and Connectivity Validation
* **CLI Command:**
```bash
# 1. IP and default route check in sandbox clone
incus exec c0-web-v-${VERIFY_UUID} -- ip addr show eth0
incus exec c0-web-v-${VERIFY_UUID} -- ip route
# 2. Ping test towards OVN logical gateway (10.0.1.1)
incus exec c0-web-v-${VERIFY_UUID} -- ping -c 2 10.0.1.1
# 3. Internet connectivity test and TCP application verification
incus exec c0-web-v-${VERIFY_UUID} -- ping -c 2 1.1.1.1
incus exec c0-web-v-${VERIFY_UUID} -- apt-get update
```
* **Operational Purpose:** Verifies IP `10.0.1.2/24`, reachability of OVN gateway `10.0.1.1`, and full TCP WAN traffic transit.
* **Theoretical Keywords and Explanation:**
    * **In-Line Verification**: Dynamic execution of tests and trial exploits on duplicated environments without crashing live service.

---

#### PHASE 5: Atomic Sandbox Teardown

##### Step 5.1: Destruction of Ephemeral Test Devices
* **CLI Command:**
```bash
incus delete c0-web-v-${VERIFY_UUID} --force
incus network delete verify-ovn-${VERIFY_UUID}
```
* **Operational Purpose:** Deletes cloned container and stops OVN sandbox network, restoring host cleanliness.
* **Theoretical Keywords and Explanation:**
    * **Atomic Teardown**: Reverse-order cleanup procedure guaranteeing complete removal of all temporary resources allocated during verification.

---

#### 🛠️ Troubleshooting Section: Register of Integrated Solutions

```
+-----------------------------------------------------------------------------------------------------------------------+
|                                    SUMMARY TABLE OF FIXES AND TROUBLESHOOTING                                        |
+------------------------------------+-------------------------------------------+--------------------------------------+
| OBSERVED ANOMALY                   | ROOT CAUSE                                | APPLIED SOLUTION IN GUIDE            |
+------------------------------------+-------------------------------------------+--------------------------------------+
| 'Failed to connect to OVN'         | Missing OVN listening socket/TCP port     | Applied 'ovn-nbctl set-connection    |
| during 'incus network create'      | or empty configuration in Incus.          | ptcp:6641:127.0.0.1' and wrote to DB.|
+------------------------------------+-------------------------------------------+--------------------------------------+
| 'Missing required                  | Incus lacks IP pool for OVN router        | Executed 'incus network set incusbr0 |
| ipv4.ovn.ranges'                   | uplink on parent network.                 | ipv4.ovn.ranges=172.16.0.100-199'.   |
+------------------------------------+-------------------------------------------+--------------------------------------+
| 'ipv4.dhcp.ranges cannot be empty' | Setting OVN ranges requires presence      | Added 'ipv4.dhcp.ranges=172.16.      |
| setting OVN ranges                 | of a valid DHCP pool.                     | 0.2-172.16.0.99' in same command.    |
+------------------------------------+-------------------------------------------+--------------------------------------+
| 'Invalid option for network:       | OVN networks use 'network=' attribute     | Replaced 'parent=incusbr0' with      |
| option parent'                     | to specify underlay uplink.               | 'network=incusbr0'.                  |
+------------------------------------+-------------------------------------------+--------------------------------------+
| 'Device validation failed eth0:    | Clone inherits 'parent=br-team-0' from    | Added 'parent=' to override command  |
| Invalid device option parent'      | profile, generating conflict with OVN.    | to reset value.                      |
+------------------------------------+-------------------------------------------+--------------------------------------+
| Container missing IP (`eth0`)      | `br-team-0` is an unmanaged Linux Bridge  | Injected static IP 10.0.1.2/24       |
| at production launch               | without active DHCP server.               | via Netplan/cloud-init in profile.   |
+------------------------------------+-------------------------------------------+--------------------------------------+
| Ping OK, but TCP commands          | Path MTU Blackhole: large TCP packets     | Added TCP MSS Clamping rule in       |
| (`apt-get`) freeze                 | exceed WAN or tunnel MTU.                 | nftables: 'maxseg size set rt mtu'.  |
+------------------------------------+-------------------------------------------+--------------------------------------+
```

---

Architectural Compliance Statement: This document complies with the specifications of the **BlueAgent v2** system.