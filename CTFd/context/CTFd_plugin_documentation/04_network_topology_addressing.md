# BlueAgent CTFd Plugin: Network Topology & Addressing Scheme

## Mathematical Model: `team_k` (1..254)

Each registered team or solo user is deterministically mapped to integer `k` in range `[1, 254]`.
Index `0` is strictly reserved for host routing and central infrastructure.

```
Subnet Supernet per Team: 10.k.0.0/16
├── VPN Plane (WireGuard wg-k):          10.k.0.0/24
└── Container Plane (Bridge br-team-k):  10.k.1.0/24
```

---

## Detailed Subnet Allocation

| Interface / Component | Subnet / IP | Description |
|---|---|---|
| WireGuard Interface (`wg-k`) | `10.k.0.1/24` | Linux host kernel endpoint; VPN gateway for team |
| Player VPN Clients | `10.k.0.2` – `10.k.0.254` | Allocated sequentially per member via Curve25519 keypair |
| Team Bridge (`br-team-k`) | `10.k.1.1/24` | Host Linux bridge connecting Incus challenge containers |
| Challenge Containers | `10.k.1.2` – `10.k.1.249` | Assigned via static `ip_suffix` defined in ChallengeConfig |
| Ephemeral Verifier Probe | `10.k.1.250` | Fixed static IP on dark OVN switch running exploit scripts |
| Host Reserved | `10.k.1.251` – `10.k.1.254` | Reserved for internal debugging and gateway expansion |

---

## Traffic Flow & Routing Rules

```
[Player Laptop]
      │
      │ WireGuard L3 UDP Tunnel (Endpoint: Host:51820+k)
      v
[Linux Host: wg-k (10.k.0.1)]
      │
      │ Kernel IP Forwarding + iptables FORWARD
      v
[Linux Bridge: br-team-k (10.k.1.1)]
      │
      ├──> [Vulnerable Container 1: 10.k.1.10]
      └──> [Vulnerable Container 2: 10.k.1.20]
```

### Security & Cross-Talk Protections
1. **Inter-Team Isolation**: Firewall rules reject packets between `10.A.0.0/16` and `10.B.0.0/16` when `A != B`.
2. **Player Isolation from Verifier**: Verifier IP `10.k.1.250` only exists during active verification on ephemeral OVN logical switch, completely disconnected from `br-team-k` and player VPN.
3. **No External Egress**: Challenge containers run with `verify_internet_access: false` by default, blocking outbound connections to prevent data exfiltration.
