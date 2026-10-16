# Network Architecture: Hybrid Production & OVN Sandbox

## 1. Architectural Model
BlueAgent splits networking into two isolated domains:
1. **Production (Live):** Native Linux kernel primitives (WireGuard + Bridge) for zero-overhead, bare-metal throughput and simple `tcpdump` monitoring.
2. **Verification (Sandbox):** Ephemeral Incus + OVN SDN overlay for sub-second, isolated testing without host IP overlap.

---

## 2. Production Domain (Live Kernel)

| Component | Interface / Resource | Addressing | Description |
| :--- | :--- | :--- | :--- |
| **L3 VPN** | `wg-k` (WireGuard) | `10.k.0.0/24` | Gateway: `10.k.0.1`. Client IPs: `10.k.0.2`–`10.k.0.254` (cap 253 members, hole-filling). Cryptokey Routing binds `/32` to peer public keys. `0 <= team_k <= 254`. Port: `51820 + team_k`. |
| **L2 Bridge** | `br-team-k` (Linux Bridge) | `10.k.1.1/24` | Dedicated per-team L2 switch acting as default gateway for team containers (`0 <= team_k <= 254`). |
| **Containers**| `eth0` (veth pair) | `10.k.1.x/24` | Attached to `br-team-k`. Deterministic IPs: `.2` Web, `.3` DB, etc. |
| **Inspection**| `tcpdump -i br-team-k` | N/A | Direct packet capture of team traffic without side-channel interference. |

### Container Static IP Injection (`systemd-networkd`)
Because `br-team-k` is unmanaged (no DHCP daemon) and minimal container images omit `cloud-init`/`netplan`, static IPs (`10.k.1.x`) are configured inside containers via `systemd-networkd`:
```ini
# /etc/systemd/network/10-eth0.network
[Match]
Name=eth0
[Network]
Address=10.k.1.x/24
Gateway=10.k.1.1
DNS=1.1.1.1
DNS=8.8.8.8
```
*Advantage:* Stored directly in the container rootfs. When duplicated via ZFS CoW snapshots, clones instantly assume the identical IP on the OVN switch without cloud-init latency or reconfiguration agents.

---

## 3. Traffic Engineering & Path MTU / MSS Clamping

Encapsulation (WireGuard L3 tunnels and OVN Geneve overlays) adds packet headers that reduce path MTU:
- **WireGuard Interface MTU:** `1420`
- **OVN Geneve Encapsulation:** `1442` (host MTU - 58)

To eliminate TCP connection freezes (such as during TLS handshakes or large payload transfers) caused by Path MTU blackholes, BlueAgent enforces TCP MSS Clamping in `nftables` **before** terminal `accept` rules:

```bash
table inet blueagent {
    set team_links {
        type ifname . ifname
        flags interval
    }
    chain forward {
        type filter hook forward priority filter; policy accept;
        # MSS clamping MUST precede accept rules to prevent bypass
        tcp flags syn / syn,rst tcp option maxseg size set rt mtu
        ct state established,related accept
        iifname . oifname @team_links accept
        iifname "wg-*" oifname != "br-team-*" accept
        iifname "br-team-*" oifname != "wg-*" accept
        iifname "wg-*" oifname "br-team-*" drop
        iifname "br-team-*" oifname "wg-*" drop
    }
    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        ip saddr 10.0.0.0/8 ip daddr != 10.0.0.0/8 masquerade
    }
}
```

---

## 4. Host Infrastructure Prerequisites

Before Incus can provision OVN networks, the host requires local DB registration and uplink bridge range definitions:

```bash
# 1. Expose OVN Northbound DB locally & register with Incus
ovn-nbctl set-connection ptcp:6641:127.0.0.1
incus config set network.ovn.northbound_connection tcp:127.0.0.1:6641

# 2. Define parent uplink bridge (incusbr0) DHCP and OVN subnet ranges
incus network set incusbr0 ipv4.address=172.16.0.1/24 ipv4.nat=true
incus network set incusbr0 ipv4.dhcp.ranges=172.16.0.2-172.16.0.99 ipv4.ovn.ranges=172.16.0.100-172.16.0.199
```

---

## 5. Verification Domain (Incus + OVN Overlay)

When automated verification is requested (`POST /teams/{team_k}/verificator`):

1. **Ephemeral Dark OVN L2 Switch Creation:**
   - Network Name: `verify-{session_id[:4]}` (strictly capped to $\le 11$ characters by Incus).
   - Type: `ovn`, isolated dark L2 switch (`ipv4.address: none`, without uplink `network` parameter).
   - Zero Host Route Overlap: Because `br-team-k` already occupies `10.k.1.1/24` in host routing table, dark L2 switch avoids `External subnet overlaps with another network or NIC` conflict.
2. **Sub-Second CoW Duplication (~100 ms):**
   - Source instances cloned via ZFS/Btrfs CoW snapshot (`POST /instances` with `source.type="copy"`).
3. **NIC Interface Re-binding (`eth0`):**
   - Live bridge binding is overridden to point to the dark OVN network:
     - **Incus API (`PATCH /1.0/instances/{name}`):**
       ```json
       {"devices": {"eth0": {"type": "nic", "network": "verify-xxxx"}}}
       ```
   - Clones retain single **`eth0`** interface. Static IP (`10.k.1.x`) is assigned inside container namespace.
4. **Transparent IP Overlap:**
   - OVN Logical Switch handles L2 switching in OVS datapaths (Geneve).
   - Hundreds of concurrent sandboxes can share identical `10.k.1.0/24` addressing without host routing table collisions.
5. **Reverse LIFO Teardown:**
   - Stop and delete verifier container -> stop and delete target clones -> delete `verify-{session_id[:4]}` network. Complete cleanup guaranteed even on assertion or script failure.