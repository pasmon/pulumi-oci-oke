"""Unit tests for the networking rules.

These exercise the pure rule builders. Resource wiring is covered by the
Pulumi engine mock tests in ``test_cluster.py``.
"""

import pytest

from oke import networking
from tests.conftest import build_config


class TestDnsLabel:
    """OCI DNS labels are short and cannot start or end with a hyphen."""

    def test_valid_label_is_returned(self):
        assert networking.dns_label("radiooke") == "radiooke"

    def test_empty_label_is_rejected(self):
        with pytest.raises(ValueError, match="1-15 characters"):
            networking.dns_label("")

    def test_long_label_is_rejected(self):
        with pytest.raises(ValueError, match="1-15 characters"):
            networking.dns_label("a" * 16)

    def test_leading_hyphen_is_rejected(self):
        with pytest.raises(ValueError, match="not start or end with a hyphen"):
            networking.dns_label("-radio")

    def test_underscore_is_rejected(self):
        with pytest.raises(ValueError, match="alphanumeric"):
            networking.dns_label("radio_oke")


class TestIngressRules:
    """The node subnet opens exactly what the design calls for, and no more."""

    def test_vcn_and_load_balancer_ports_always_present(self):
        rules = networking.build_ingress_rules("10.10.0.0/16")
        sources = [rule.source for rule in rules]
        assert "10.10.0.0/16" in sources
        assert "0.0.0.0/0" in sources
        assert len(rules) == 2

    def test_load_balancer_backend_range_is_tcp_30000_32767(self):
        rules = networking.build_ingress_rules("10.10.0.0/16")
        lb_rules = [rule for rule in rules if rule.protocol == "6"]
        assert len(lb_rules) == 1
        assert lb_rules[0].tcp_options.max == networking.LB_BACKEND_PORT_MAX
        assert lb_rules[0].tcp_options.min == networking.LB_BACKEND_PORT_MIN

    def test_wireguard_never_opens_an_ingress_port(self):
        # The nodes are public and the peer is the operator's router, so the
        # tunnel is outbound and its return path is stateful. There is nothing
        # for an internet-facing UDP rule to admit.
        rules = networking.build_ingress_rules("10.10.0.0/16")
        assert not [rule for rule in rules if rule.udp_options]

    def test_ssh_is_closed_unless_explicitly_enabled(self):
        rules = networking.build_ingress_rules("10.10.0.0/16")
        assert not [
            rule
            for rule in rules
            if rule.protocol == "6" and rule.tcp_options.min == networking.SSH_PORT
        ]

        rules = networking.build_ingress_rules("10.10.0.0/16", ssh_from_anywhere=True)
        ssh_rules = [
            rule
            for rule in rules
            if rule.tcp_options and rule.tcp_options.min == networking.SSH_PORT
        ]
        assert len(ssh_rules) == 1
        assert ssh_rules[0].source == "0.0.0.0/0"

    def test_all_rules_are_stateful(self):
        rules = networking.build_ingress_rules("10.10.0.0/16", True)
        assert all(rule.stateless is False for rule in rules)


class TestConstants:
    """The fixed values the design depends on."""

    def test_kube_api_port(self):
        assert networking.KUBE_API_PORT == 6443

    def test_load_balancer_backend_range(self):
        assert networking.LB_BACKEND_PORT_MIN == 30000
        assert networking.LB_BACKEND_PORT_MAX == 32767


class TestDefaultTopology:
    """The default CIDRs match the documented layout."""

    def test_vcn_and_subnets(self):
        cfg = build_config()
        assert cfg.vcn_cidr == "10.10.0.0/16"
        assert cfg.endpoint_subnet_cidr == "10.10.0.0/24"
        assert cfg.nodes_subnet_cidr == "10.10.1.0/24"
        # A third subnet, because the load balancer subnet cannot be the node
        # subnet: OKE rejects a node pool placed in a service subnet.
        assert cfg.service_subnet_cidr == "10.10.2.0/24"

    def test_pod_and_service_ranges_sit_outside_the_vcn(self):
        cfg = build_config()
        assert cfg.pods_cidr == "10.244.0.0/16"
        assert cfg.services_cidr == "10.96.0.0/16"
