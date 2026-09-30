"""Unit tests for the configuration validation rules."""

import pytest

from tests.conftest import build_config


class TestCidrValidation:
    """Network ranges must be valid, distinct and correctly nested."""

    def test_defaults_are_accepted(self):
        cfg = build_config()
        assert str(cfg.vcn_cidr) == "10.10.0.0/16"

    def test_subnet_outside_vcn_is_rejected(self):
        with pytest.raises(ValueError, match="not inside vcn-cidr"):
            build_config(vcn_cidr="10.10.0.0/16", nodes_subnet_cidr="192.168.0.0/24")

    def test_pods_cidr_inside_the_vcn_is_rejected(self):
        with pytest.raises(ValueError, match="overlaps vcn-cidr"):
            build_config(vcn_cidr="10.10.0.0/16", pods_cidr="10.10.5.0/24")

    def test_services_cidr_inside_the_vcn_is_rejected(self):
        with pytest.raises(ValueError, match="overlaps vcn-cidr"):
            build_config(vcn_cidr="10.10.0.0/16", services_cidr="10.10.9.0/24")

    def test_pods_and_services_overlapping_each_other_is_rejected(self):
        with pytest.raises(ValueError, match="overlaps"):
            build_config(pods_cidr="10.244.0.0/16", services_cidr="10.244.1.0/24")

    def test_malformed_cidr_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid vcn-cidr"):
            build_config(vcn_cidr="10.10.0.0/99")

    def test_identical_subnets_are_rejected(self):
        # Reported as an overlap, since identical ranges do overlap.
        with pytest.raises(ValueError, match="overlaps"):
            build_config(
                endpoint_subnet_cidr="10.10.0.0/24",
                nodes_subnet_cidr="10.10.0.0/24",
            )

    def test_vcn_containing_the_subnets_is_accepted(self):
        # The VCN deliberately encloses both subnets, which is not an overlap.
        cfg = build_config(
            vcn_cidr="10.10.0.0/16",
            endpoint_subnet_cidr="10.10.0.0/24",
            nodes_subnet_cidr="10.10.1.0/24",
        )
        assert cfg.endpoint_subnet_cidr == "10.10.0.0/24"


class TestNodeSizing:
    """Node sizing is validated, with a warning rather than a block."""

    def test_totals_are_computed(self):
        cfg = build_config(node_count=2, node_ocpus=2, node_memory_gbs=12)
        assert cfg.node_total_ocpus == 4
        assert cfg.node_total_memory_gbs == 24

    def test_over_always_free_warns_but_does_not_raise(self, capsys):
        cfg = build_config(node_count=2, node_ocpus=2, node_memory_gbs=12)
        assert cfg.node_total_ocpus == 4
        output = capsys.readouterr().out
        assert "COST WARNING" in output
        assert "Always Free allows: 2 OCPU / 12 GB" in output
        assert "node-ocpus 1" in output

    def test_within_always_free_does_not_warn(self, capsys):
        build_config(node_count=2, node_ocpus=1, node_memory_gbs=6)
        assert "COST WARNING" not in capsys.readouterr().out

    def test_single_node_at_full_allowance_does_not_warn(self, capsys):
        build_config(node_count=1, node_ocpus=2, node_memory_gbs=12)
        assert "COST WARNING" not in capsys.readouterr().out

    def test_zero_nodes_is_rejected(self):
        with pytest.raises(ValueError, match="node-count must be at least 1"):
            build_config(node_count=0)

    def test_zero_ocpus_is_rejected(self):
        with pytest.raises(ValueError, match="node-ocpus must be at least 1"):
            build_config(node_ocpus=0)

    def test_boot_volume_below_oci_minimum_is_rejected(self):
        with pytest.raises(
            ValueError, match="boot-volume-size-gbs must be at least 50"
        ):
            build_config(boot_volume_size_gbs=47)

    def test_block_volume_over_always_free_is_rejected(self):
        # 4 nodes x 50 GB boot volumes plus the 20 GB cache exceeds 200 GB.
        with pytest.raises(ValueError, match="Always Free block volume allowance"):
            build_config(node_count=4, boot_volume_size_gbs=50)

    def test_block_volume_budget_accounts_for_the_cache(self):
        cfg = build_config(node_count=2, boot_volume_size_gbs=50)
        # 2 x 50 GB boot volumes plus the 20 GB shared audio cache.
        assert cfg.total_block_volume_gb == 120


class TestWireguardValidation:
    """Wireguard fields are all-or-nothing."""

    def test_disabled_by_default(self):
        assert build_config().wireguard_enabled is False

    def test_endpoint_alone_is_rejected(self):
        with pytest.raises(ValueError, match="together, or omit them all"):
            build_config(wireguard_peer_endpoint="198.51.100.7")

    def test_missing_preshared_key_is_rejected(self):
        with pytest.raises(ValueError, match="wireguard-preshared-key"):
            build_config(
                wireguard_peer_endpoint="198.51.100.7",
                wireguard_peer_public_key="peerPublicKey=",
                wireguard_private_key="privateKey=",
            )

    def test_complete_configuration_is_accepted(self):
        cfg = build_config(
            wireguard_peer_endpoint="198.51.100.7",
            wireguard_peer_public_key="peerPublicKey=",
            wireguard_private_key="privateKey=",
            wireguard_preshared_key="presharedKey=",
            wireguard_allowed_cidrs=["192.168.88.200/32"],
        )
        assert cfg.wireguard_enabled is True
        # The router takes the first usable address in the tunnel subnet.
        assert cfg.wireguard_router_address == "10.99.0.1/24"

    def test_tunnel_must_not_overlap_the_cluster(self):
        with pytest.raises(ValueError, match="overlaps"):
            build_config(
                wireguard_peer_endpoint="198.51.100.7",
                wireguard_peer_public_key="peerPublicKey=",
                wireguard_private_key="privateKey=",
                wireguard_preshared_key="presharedKey=",
                wireguard_subnet_cidr="10.10.0.0/24",
            )

    def test_tunnel_subnet_too_small_is_rejected(self):
        with pytest.raises(ValueError, match="too small"):
            build_config(
                wireguard_peer_endpoint="198.51.100.7",
                wireguard_peer_public_key="peerPublicKey=",
                wireguard_private_key="privateKey=",
                wireguard_preshared_key="presharedKey=",
                wireguard_subnet_cidr="10.99.0.0/30",
            )

    def test_invalid_allowed_cidr_is_rejected(self):
        with pytest.raises(ValueError, match="wireguard-allowed-cidrs"):
            build_config(
                wireguard_peer_endpoint="198.51.100.7",
                wireguard_peer_public_key="peerPublicKey=",
                wireguard_private_key="privateKey=",
                wireguard_preshared_key="presharedKey=",
                wireguard_allowed_cidrs=["not-a-cidr"],
            )


class TestTlsValidation:
    """TLS fields must be set together."""

    def test_domain_alone_is_rejected(self):
        with pytest.raises(
            ValueError, match="tls-domain and cloudflare-email together"
        ):
            build_config(tls_domain="radio.example.com")

    def test_email_alone_is_rejected(self):
        with pytest.raises(
            ValueError, match="tls-domain and cloudflare-email together"
        ):
            build_config(cloudflare_email="you@example.com")

    def test_both_is_accepted(self):
        cfg = build_config(
            tls_domain="radio.example.com", cloudflare_email="you@example.com"
        )
        assert cfg.tls_domain == "radio.example.com"


class TestOcidValidation:
    """Malformed OCIDs are caught early rather than failing inside OCI."""

    def test_malformed_tenancy_is_rejected(self):
        with pytest.raises(
            ValueError, match="tenancy-id .* does not look like an OCID"
        ):
            build_config(tenancy_id="not-an-ocid")

    def test_malformed_vault_is_rejected(self):
        with pytest.raises(ValueError, match="vault-id .* does not look like an OCID"):
            build_config(vault_id="ocid1.vault")

    def test_well_formed_ocids_are_accepted(self):
        cfg = build_config(
            tenancy_id="ocid1.tenancy.oc1..testtenancy0000000000000000000000000000000",
            vault_id="ocid1.vault.oc1.eu-stockholm-1.aaaaaaaaexample",
        )
        assert cfg.vault_id.startswith("ocid1.vault.oc1.eu-stockholm-1")


class TestArgoCdDefaults:
    """Argo CD defaults keep the single-owner and no-exposure rules."""

    def test_managed_by_pulumi_by_default(self):
        assert build_config().argocd_managed_by_pulumi is True

    def test_handoff_flag_parses(self):
        assert (
            build_config(
                **{"argocd-managed-by-pulumi": "false"}
            ).argocd_managed_by_pulumi
            is False
        )

    def test_ssh_is_closed_by_default(self):
        assert build_config().ssh_from_anywhere is False
