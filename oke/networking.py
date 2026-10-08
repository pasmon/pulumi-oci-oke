"""VCN, internet gateway, route table, security lists and the three subnets.

Both subnets are public. That is deliberate: the OCI cloud-controller-manager
creates the Envoy Gateway load balancer in the subnet of its backends, so a
public node subnet hands the load balancer a public IP without needing an
``oci-load-balancer-subnet-id`` annotation. Keeping that annotation out of the
chart values is what lets ``EnvoyProxy`` stay static YAML owned by argo-apps.
"""

import pulumi
import pulumi_oci as oci

# Kubernetes API server port, opened to the internet on the endpoint subnet.
KUBE_API_PORT = 6443

# Public Envoy Gateway listener.
HTTPS_PORT = 443

# OCI flexible load balancer backend ports. The load balancer health checks and
# the connections to the Envoy pods both arrive on this range.
LB_BACKEND_PORT_MIN = 30000
LB_BACKEND_PORT_MAX = 32767

SSH_PORT = 22

VCN_DNS_LABEL_MAX = 15


def dns_label(value):
    """Validate and return an OCI DNS label.

    OCI accepts 1-15 characters of letters, digits and hyphens, not starting or
    ending with a hyphen.
    """
    if not 1 <= len(value) <= VCN_DNS_LABEL_MAX:
        raise ValueError(
            f"vcn-dns-label must be 1-{VCN_DNS_LABEL_MAX} characters, got {value!r}"
        )
    if value.startswith("-") or value.endswith("-"):
        raise ValueError(
            f"vcn-dns-label must not start or end with a hyphen, got {value!r}"
        )
    if not all(character.isalnum() or character == "-" for character in value):
        raise ValueError(
            f"vcn-dns-label must be alphanumeric with hyphens, got {value!r}"
        )
    return value


def _ingress_rule(protocol, source, min_port=None, max_port=None, transport=None):
    """Build one stateful ingress rule, wiring up the port options when needed.

    Args:
        protocol: the IP protocol number, or "all".
        source: the CIDR the traffic originates from.
        min_port: lowest port in the range, if the rule is port scoped.
        max_port: highest port in the range, if the rule is port scoped.
        transport: "tcp" or "udp" when the rule needs port options.
    """
    tcp = transport == "tcp"
    udp = transport == "udp"
    options = {}
    if tcp and (min_port is not None or max_port is not None):
        options["tcp_options"] = oci.core.SecurityListIngressSecurityRuleTcpOptionsArgs(
            max=max_port, min=min_port
        )
    if udp and (min_port is not None or max_port is not None):
        options["udp_options"] = oci.core.SecurityListIngressSecurityRuleUdpOptionsArgs(
            max=max_port, min=min_port
        )

    return oci.core.SecurityListIngressSecurityRuleArgs(
        protocol=str(protocol),
        source=source,
        source_type="CIDR_BLOCK",
        stateless=False,
        **options,
    )


def build_ingress_rules(vcn_cidr, ssh_from_anywhere=False):
    """Build the ingress rules for the worker node subnet.

    There is deliberately no Wireguard rule. The nodes hold public IPs and the
    peer is the operator's router, so every tunnel packet is either outbound or
    the stateful return of an outbound one. Opening UDP to the internet would
    only widen the node subnet for no reachable path.

    Args:
        vcn_cidr: the VCN CIDR, allowed to reach nodes on every port.
        ssh_from_anywhere: when true, opens SSH to the internet.

    Returns:
        A list of ingress rule argument objects.
    """
    rules = [
        # Nodes, pods and the API endpoint all live in the VCN.
        _ingress_rule("all", vcn_cidr),
        # OCI flexible load balancer backends and health checks.
        _ingress_rule(
            "6",
            "0.0.0.0/0",
            min_port=LB_BACKEND_PORT_MIN,
            max_port=LB_BACKEND_PORT_MAX,
            transport="tcp",
        ),
    ]

    if ssh_from_anywhere:
        rules.append(
            _ingress_rule(
                "6", "0.0.0.0/0", min_port=SSH_PORT, max_port=SSH_PORT, transport="tcp"
            )
        )

    return rules


def build_service_ingress_rules(vcn_cidr):
    """Build ingress rules for the public load balancer subnet."""
    return [
        _ingress_rule("all", vcn_cidr),
        # The public Envoy listener terminates HTTPS on the load balancer.
        _ingress_rule(
            "6",
            "0.0.0.0/0",
            min_port=HTTPS_PORT,
            max_port=HTTPS_PORT,
            transport="tcp",
        ),
        # OCI flexible load balancer backends and health checks.
        _ingress_rule(
            "6",
            "0.0.0.0/0",
            min_port=LB_BACKEND_PORT_MIN,
            max_port=LB_BACKEND_PORT_MAX,
            transport="tcp",
        ),
    ]


def create_network(cfg):
    """Create the VCN, gateway, route table, security lists and subnets."""
    label = dns_label(cfg.vcn_dns_label)

    vcn = oci.core.Vcn(
        "oke-vcn",
        compartment_id=cfg.compartment_id,
        cidr_block=cfg.vcn_cidr,
        dns_label=label,
    )

    internet_gateway = oci.core.InternetGateway(
        "oke-internetgateway",
        compartment_id=cfg.compartment_id,
        vcn_id=vcn.id,
        enabled=True,
    )

    route_table = oci.core.DefaultRouteTable(
        "oke-routetable",
        compartment_id=cfg.compartment_id,
        manage_default_resource_id=vcn.default_route_table_id,
        route_rules=[
            oci.core.DefaultRouteTableRouteRuleArgs(
                network_entity_id=internet_gateway.id,
                destination="0.0.0.0/0",
            )
        ],
        opts=pulumi.ResourceOptions(depends_on=vcn),
    )

    # The endpoint subnet carries only the Kubernetes API. Everything else is
    # reachable from inside the VCN, and 6443 is open to the internet because
    # the OKE control plane lives outside the VCN.
    endpoint_security_list = oci.core.SecurityList(
        "oke-endpoint-securitylist",
        compartment_id=cfg.compartment_id,
        vcn_id=vcn.id,
        display_name="oke-endpoint-securitylist",
        ingress_security_rules=[
            _ingress_rule("all", cfg.vcn_cidr),
            _ingress_rule(
                "6",
                "0.0.0.0/0",
                min_port=KUBE_API_PORT,
                max_port=KUBE_API_PORT,
                transport="tcp",
            ),
        ],
        egress_security_rules=[
            oci.core.SecurityListEgressSecurityRuleArgs(
                protocol="all",
                destination="0.0.0.0/0",
                destination_type="CIDR_BLOCK",
                stateless=False,
            )
        ],
    )

    node_security_list = oci.core.SecurityList(
        "oke-nodes-securitylist",
        compartment_id=cfg.compartment_id,
        vcn_id=vcn.id,
        display_name="oke-nodes-securitylist",
        ingress_security_rules=build_ingress_rules(
            cfg.vcn_cidr,
            ssh_from_anywhere=cfg.ssh_from_anywhere,
        ),
        egress_security_rules=[
            oci.core.SecurityListEgressSecurityRuleArgs(
                protocol="all",
                destination="0.0.0.0/0",
                destination_type="CIDR_BLOCK",
                stateless=False,
            )
        ],
    )

    endpoint_subnet = oci.core.Subnet(
        "oke-endpoint-subnet",
        compartment_id=cfg.compartment_id,
        vcn_id=vcn.id,
        cidr_block=cfg.endpoint_subnet_cidr,
        display_name="oke-endpoint-subnet",
        route_table_id=route_table.id,
        security_list_ids=[endpoint_security_list.id],
    )

    # Deliberately not prohibiting public IPs. See the module docstring.
    nodes_subnet = oci.core.Subnet(
        "oke-nodes-subnet",
        compartment_id=cfg.compartment_id,
        vcn_id=vcn.id,
        cidr_block=cfg.nodes_subnet_cidr,
        display_name="oke-nodes-subnet",
        route_table_id=route_table.id,
        security_list_ids=[node_security_list.id],
    )

    # OKE-managed load balancers get their own subnet. OKE refuses to place a
    # node pool in a subnet registered as a service load balancer subnet, so
    # reusing the node subnet here fails the node pool create.
    service_security_list = oci.core.SecurityList(
        "oke-service-securitylist",
        compartment_id=cfg.compartment_id,
        vcn_id=vcn.id,
        display_name="oke-service-securitylist",
        ingress_security_rules=build_service_ingress_rules(cfg.vcn_cidr),
        egress_security_rules=[
            oci.core.SecurityListEgressSecurityRuleArgs(
                protocol="all",
                destination="0.0.0.0/0",
                destination_type="CIDR_BLOCK",
                stateless=False,
            )
        ],
    )

    service_subnet = oci.core.Subnet(
        "oke-service-subnet",
        compartment_id=cfg.compartment_id,
        vcn_id=vcn.id,
        cidr_block=cfg.service_subnet_cidr,
        display_name="oke-service-subnet",
        route_table_id=route_table.id,
        security_list_ids=[service_security_list.id],
    )

    return {
        "vcn": vcn,
        "internet_gateway": internet_gateway,
        "route_table": route_table,
        "endpoint_security_list": endpoint_security_list,
        "nodes_security_list": node_security_list,
        "service_security_list": service_security_list,
        "endpoint_subnet": endpoint_subnet,
        "nodes_subnet": nodes_subnet,
        "service_subnet": service_subnet,
    }
