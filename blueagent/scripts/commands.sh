# open vswitch, ovn, incus, nftables
sudo apt-get update && sudo apt-get install -y openvswitch-switch ovn-central ovn-host incus wireguard iproute2 nftables

sudo apt instal wireguard

# zfs install
sudo apt install linux-headers-amd64
sudo apt install -t stable-backports zfsutils-linux

sudo reboot now

# after reboot, run the following commands

# ovs and ovn setup
sudo ovs-vsctl set open_vswitch .    external_ids:ovn-remote=unix:/run/ovn/ovnsb_db.sock    external_ids:ovn-encap-type=geneve    external_ids:ovn-encap-ip=127.0.0.1
sudo ovn-nbctl set-connection ptcp:6641:127.0.0.1 
sudo incus config set network.ovn.northbound_connection tcp:127.0.0.1:6641 

# create zfs pool
sudo incus storage create blueagent-zfs zfs

sudo incus admin init --auto

sudo incus network set incusbr0 ipv4.address=172.16.0.1/24 ipv4.nat=true
sudo incus network set incusbr0 ipv4.dhcp.ranges=172.16.0.2-172.16.0.99 ipv4.ovn.ranges=172.16.0.100-172.16.0.199

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

sudo incus profile create team-0-profile 2>/dev/null || true
sudo incus profile device add team-0-profile eth0 nic nictype=bridged parent=br-team-0 name=eth0 2>/dev/null || true

# 2. Netplan injection for static IP (10.0.1.2)
sudo incus profile set team-0-profile user.network-config="
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
" 2>/dev/null || true

sudo incus launch ubuntu:22.04 c0-web --profile default --profile team-0-profile --storage blueagent-zfs

# assign ip address
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

# create verify network
VERIFY_UUID=0
sudo incus network create verify-ovn-${VERIFY_UUID} --type=ovn network=incusbr0 ipv4.address=10.0.1.1/24 ipv4.nat=true
sudo incus copy c0-web c0-web-v-${VERIFY_UUID} --instance-only
sudo incus config device override c0-web-v-${VERIFY_UUID} eth0 network=verify-ovn-${VERIFY_UUID} parent=
sudo incus start c0-web-v-${VERIFY_UUID}