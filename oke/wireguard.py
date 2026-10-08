"""Optional Wireguard tunnel from the worker nodes to the home router.

Both OKE nodes are workers and share one identical ``node_metadata.user_data``,
so a fixed tunnel address per node is impossible. The boot script instead
derives the tunnel address from the node's own private IP::

    tunnel_ip = 10.99.0.<last octet of the node's private IP>

Both nodes stay inside the tunnel subnet, the router keeps the first usable
address, and the router needs no change: it already holds a single peer entry
for the whole cluster with the same shared private key.

The masquerade source range is the cluster's pods CIDR, so pod traffic leaving
through the tunnel is collapsed onto the node's tunnel address and the
Raspberry Pi's network guard still accepts it.
"""

import base64
import ipaddress

import pulumi

WIREGUARD_INTERFACE = "wg0"

# Nodes are behind NAT, so without this the router cannot open a return path.
WIREGUARD_PERSISTENT_KEEPALIVE = 25


def node_tunnel_address(private_ip, subnet_cidr):
    """Derive a node's tunnel address from its private IP.

    Args:
        private_ip: the node's private IP inside the VCN.
        subnet_cidr: the tunnel subnet, which must have at least four addresses.

    Returns:
        An address string with a prefix length, for example ``10.99.0.11/24``.

    Raises:
        ValueError: if the subnet is too small or the derived address would
            collide with the router.
    """
    network = ipaddress.ip_network(subnet_cidr, strict=False)

    address = ipaddress.ip_address(private_ip)
    last_octet = int(str(address).rsplit(".", 1)[-1])

    # Keep the tunnel subnet prefix but borrow the host octet from the node's
    # private IP, which is what makes the address unique across the pool.
    candidate = ipaddress.ip_address(
        f"{str(network.network_address).rsplit('.', 1)[0]}.{last_octet}"
    )

    if candidate not in network:
        raise ValueError(
            f"derived tunnel address {candidate} is outside the tunnel subnet {network}"
        )
    if candidate == network.network_address + 1:
        raise ValueError(
            f"derived tunnel address {candidate} collides with the router address; "
            "choose a different nodes-subnet-cidr"
        )

    return f"{candidate}/{network.prefixlen}"


def wireguard_allowed_ips(cfg):
    """Build the peer's AllowedIPs list for crypto-routing.

    Wireguard only sends a packet into the tunnel when its destination matches
    an AllowedIPs entry, so this list is what makes the routed LAN prefixes
    reachable. The tunnel subnet always comes first so the router itself stays
    reachable.
    """
    return ", ".join([cfg.wireguard_subnet_cidr, *cfg.wireguard_allowed_cidrs])


def build_wireguard_command(cfg, pods_cidr):
    """Build the Wireguard setup script for one node.

    The same script runs on every node. It reads the instance metadata for its
    own private IP and derives the tunnel address from it.

    The two keys are secret Outputs, so the whole script is built in an apply.
    Formatting an Output writes the stringified-Output warning into wg0.conf
    instead of the key, which fails authentication at the router rather than at
    boot.

    Args:
        cfg: the validated :class:`oke.config.Config`.
        pods_cidr: the cluster pods CIDR, used for the masquerade rule.

    Returns:
        The shell script, or an ``Output[str]`` of it when a key is secret.
    """
    keys = [cfg.wireguard_private_key, cfg.wireguard_preshared_key]
    if any(isinstance(key, pulumi.Output) for key in keys):
        return pulumi.Output.all(*keys).apply(
            lambda resolved: _wireguard_script(cfg, pods_cidr, *resolved)
        )
    return _wireguard_script(cfg, pods_cidr, *keys)


def _wireguard_script(cfg, pods_cidr, private_key, preshared_key):
    """Render the setup script once the secret keys are resolved."""
    return f"""set -eu
# OCI instance metadata service, reachable from every node on the metadata
# endpoint. Used to discover this node's own private IP so the tunnel address
# is unique across the pool.
PRIVATE_IP=$(curl -s -H "Authorization: Bearer Oracle" \\
  http://169.254.169.254/opc/v2/vnic/ | grep -o '"privateIp"[^,]*' | head -1 | cut -d'"' -f4)
test -n "$PRIVATE_IP"

TUNNEL_SUBNET="{cfg.wireguard_subnet_cidr}"
LAST_OCTET=$(echo "$PRIVATE_IP" | cut -d. -f4)
PREFIX=$(echo "$TUNNEL_SUBNET" | cut -d/ -f2)
TUNNEL_BASE=$(echo "$TUNNEL_SUBNET" | cut -d/ -f1 | cut -d. -f1-3)
ADDRESS="$TUNNEL_BASE.$LAST_OCTET/$PREFIX"

# Oracle Linux, not Debian: the node pool image is OL8 (see cluster.NODE_OS_TYPE),
# which has dnf and no apt-get. Failing here used to abort user data under
# `set -eu`, and a node whose user data fails never joins the cluster. So a
# missing package costs the tunnel, not the node.
dnf install -y wireguard-tools || {{
  echo "WARNING: wireguard-tools unavailable, no tunnel on this node" >&2
  exit 0
}}
install -d -m 700 /etc/wireguard
install -m 644 /dev/null /etc/sysctl.d/99-{WIREGUARD_INTERFACE}.conf
tee /etc/sysctl.d/99-{WIREGUARD_INTERFACE}.conf << 'EOF' > /dev/null
net.ipv4.ip_forward = 1
EOF
sysctl --system > /dev/null

tee /etc/wireguard/{WIREGUARD_INTERFACE}.conf << EOF > /dev/null
[Interface]
Address = $ADDRESS
ListenPort = {cfg.wireguard_listen_port}
PrivateKey = {private_key}
PostUp = iptables -t nat -A POSTROUTING -s {pods_cidr} -o {WIREGUARD_INTERFACE} -j MASQUERADE
PostUp = iptables -I FORWARD -i {WIREGUARD_INTERFACE} -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -s {pods_cidr} -o {WIREGUARD_INTERFACE} -j MASQUERADE
PostDown = iptables -D FORWARD -i {WIREGUARD_INTERFACE} -j ACCEPT

[Peer]
PublicKey = {cfg.wireguard_peer_public_key}
PresharedKey = {preshared_key}
Endpoint = {cfg.wireguard_peer_endpoint}:{cfg.wireguard_listen_port}
AllowedIPs = {wireguard_allowed_ips(cfg)}
PersistentKeepalive = {WIREGUARD_PERSISTENT_KEEPALIVE}
EOF

chmod 600 /etc/wireguard/{WIREGUARD_INTERFACE}.conf
systemctl enable wg-quick@{WIREGUARD_INTERFACE}
systemctl restart wg-quick@{WIREGUARD_INTERFACE}
# wg-quick reports active as soon as the link is configured, so this normally
# returns immediately and needs no peer. Bound it anyway and warn rather than
# abort: the same reasoning as the package install above.
timeout 60 systemctl is-active --wait wg-quick@{WIREGUARD_INTERFACE} \\
  || echo "WARNING: {WIREGUARD_INTERFACE} did not come up" >&2
"""


def build_node_user_data(cfg):
    """Build the base64 node user data, or ``None`` when Wireguard is off."""
    if not cfg.wireguard_enabled:
        return None
    script = build_wireguard_command(cfg, cfg.pods_cidr)
    if not isinstance(script, pulumi.Output):
        return _encode(script)
    return script.apply(_encode)


def _encode(script):
    """Base64 the script, because OKE node user data is not plain text."""
    return base64.b64encode(script.encode("utf-8")).decode("utf-8")
