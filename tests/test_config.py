"""Unit tests for the configuration validation rules."""

import inspect

import pytest

from oke.config import (
    ALWAYS_FREE_BLOCK_VOLUME_GB,
    AUDIO_CACHE_GB,
    DATABASE_VOLUME_GB,
    Config,
)
from tests.conftest import FakeConfig as _FakeConfig
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
        # The VCN deliberately encloses all three subnets, which is not an overlap.
        cfg = build_config(
            vcn_cidr="10.10.0.0/16",
            endpoint_subnet_cidr="10.10.0.0/24",
            nodes_subnet_cidr="10.10.1.0/24",
        )
        assert cfg.endpoint_subnet_cidr == "10.10.0.0/24"

    def test_service_subnet_must_differ_from_the_node_subnet(self):
        # OKE rejects a node pool in a service load balancer subnet, so sharing
        # one range fails at apply rather than at preview.
        with pytest.raises(ValueError, match="overlaps"):
            build_config(
                nodes_subnet_cidr="10.10.2.0/24",
                service_subnet_cidr="10.10.2.0/24",
            )

    def test_service_subnet_outside_the_vcn_is_rejected(self):
        with pytest.raises(ValueError, match="service-subnet-cidr"):
            build_config(service_subnet_cidr="192.168.9.0/24")


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
        # Scoped to the node-pool warning. This configuration is at the node
        # allowance, but two nodes still consume the whole block volume
        # budget, which is warned about separately.
        assert (
            "node pool exceeds the Always Free allowance" not in capsys.readouterr().out
        )

    def test_single_node_at_full_allowance_does_not_warn(self, capsys):
        build_config(node_count=1, node_ocpus=2, node_memory_gbs=12)
        output = capsys.readouterr().out
        assert "node pool exceeds the Always Free allowance" not in output
        # One node leaves 150 GB of the 200 GB allowance, so there is real
        # headroom and no block volume warning either.
        assert "no Always Free headroom" not in output

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

    def test_block_volume_budget_accounts_for_every_data_volume(self):
        cfg = build_config(node_count=2, boot_volume_size_gbs=50)
        # 2 x 50 GB boot volumes, plus the 50 GB audio cache and the 50 GB
        # database. Both data volumes are the OCI Block Volume floor, which is
        # what the tenancy is billed for rather than what the PVC requests.
        assert cfg.total_block_volume_gb == 200

    def test_block_volume_accounting_uses_provisioned_not_requested_size(self):
        """The 20Gi audio cache claim is provisioned at the 50 GB OCI minimum.

        Budgeting the requested 20 GB understated the tenancy by 30 GB and let
        a configuration that actually billed 200 GB pass as 170 GB.
        """
        assert AUDIO_CACHE_GB == 50
        assert DATABASE_VOLUME_GB == 50

    def test_defaults_reach_the_always_free_block_volume_ceiling(self, capsys):
        """Defaults sit exactly on 200 GB, so they must warn, not raise.

        Reaching the ceiling is still inside Always Free. Raising here would
        make the default configuration impossible to preview.
        """
        cfg = build_config(node_count=2, boot_volume_size_gbs=50)
        assert cfg.total_block_volume_gb == ALWAYS_FREE_BLOCK_VOLUME_GB
        assert "no Always Free headroom" in capsys.readouterr().out

    def test_block_volume_headroom_is_reported(self, capsys):
        """One node leaves 100 GB spare, so the warning stays quiet."""
        build_config(node_count=1, boot_volume_size_gbs=50)
        assert "no Always Free headroom" not in capsys.readouterr().out


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
            )

    def test_complete_configuration_is_accepted(self):
        cfg = build_config(
            wireguard_peer_endpoint="198.51.100.7",
            wireguard_peer_public_key="peerPublicKey=",
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
                wireguard_preshared_key="presharedKey=",
                wireguard_subnet_cidr="10.10.0.0/24",
            )

    def test_tunnel_subnet_too_small_is_rejected(self):
        with pytest.raises(ValueError, match="too small"):
            build_config(
                wireguard_peer_endpoint="198.51.100.7",
                wireguard_peer_public_key="peerPublicKey=",
                wireguard_preshared_key="presharedKey=",
                wireguard_subnet_cidr="10.99.0.0/30",
            )

    def test_invalid_allowed_cidr_is_rejected(self):
        with pytest.raises(ValueError, match="wireguard-allowed-cidrs"):
            build_config(
                wireguard_peer_endpoint="198.51.100.7",
                wireguard_peer_public_key="peerPublicKey=",
                wireguard_preshared_key="presharedKey=",
                wireguard_allowed_cidrs=["not-a-cidr"],
            )


class TestOcidValidation:
    """Malformed OCIDs are caught early rather than failing inside OCI."""

    def test_malformed_tenancy_is_rejected(self):
        with pytest.raises(
            ValueError, match="tenancy-id .* does not look like an OCID"
        ):
            build_config(tenancy_id="not-an-ocid")

    def test_well_formed_tenancy_is_accepted(self):
        # tenancy-id and vault-id go together: the policy is created in the
        # tenancy and scoped to the vault, so neither half works alone.
        cfg = build_config(
            tenancy_id="ocid1.tenancy.oc1..testtenancy0000000000000000000000000000000",
            vault_id="ocid1.vault.oc1.eu-stockholm-1.testvault000000000000000000000",
        )
        assert cfg.tenancy_id.startswith("ocid1.tenancy.oc1..")

    def test_malformed_vault_is_rejected(self):
        with pytest.raises(ValueError, match="vault-id .* does not look like an OCID"):
            build_config(
                tenancy_id="ocid1.tenancy.oc1..testtenancy0000000000000000000000000000000",
                vault_id="not-an-ocid",
            )

    def test_well_formed_vault_is_accepted(self):
        cfg = build_config(
            tenancy_id="ocid1.tenancy.oc1..testtenancy0000000000000000000000000000000",
            vault_id="ocid1.vault.oc1.eu-stockholm-1.testvault000000000000000000000",
        )
        assert cfg.vault_id.startswith("ocid1.vault.oc1.")


class TestRemovedConfigKeys:
    """Config keys this program never consumed are gone, not merely unused.

    Each of these read as if it configured something and did not. A key that
    nothing reads is a place for a stale value to hide, which is worse than its
    absence: the operator believes the value is in effect.
    """

    def test_tls_keys_are_not_loaded(self):
        cfg = build_config(
            tls_domain="radio.example.com", cloudflare_email="you@example.com"
        )
        assert not hasattr(cfg, "tls_domain")
        assert not hasattr(cfg, "cloudflare_email")

    def test_source_declares_no_removed_key(self):
        source = inspect.getsource(Config)
        for key in ("tls-domain", "cloudflare-email"):
            assert f'"{key}"' not in source

    def test_eso_service_account_keys_are_gone(self):
        # Instance principals need no Kubernetes ServiceAccount, so the keys that
        # named one are removed rather than left unread.
        source = inspect.getsource(Config)
        for key in ("eso-service-account-name", "eso-service-account-namespace"):
            assert f'"{key}"' not in source


class TestGitopsHandoverConfig:
    """The hand-over target is the GitOps repo's own entrypoint."""

    def test_default_path_is_the_app_of_apps_directory(self):
        # Not a bootstrap tree in this repo. The GitOps repository declares its
        # own entrypoint, so Pulumi does not get a say in it.
        assert build_config().argocd_repo_path == "app-of-apps"

    def test_path_is_overridable(self):
        cfg = build_config(argocd_repo_path="some/other/path")
        assert cfg.argocd_repo_path == "some/other/path"

    def test_repo_url_is_required(self):
        # A missing URL has no default: pointing the hand-over at the wrong
        # repository is the one mistake that must not be made silently.
        with pytest.raises(KeyError):
            Config(
                _FakeConfig(
                    {
                        "compartment-id": "ocid1.compartment.oc1..test",
                        "ssh-public-key-path": "unused",
                    }
                )
            )


class TestArgoCdDefaults:
    """Argo CD defaults keep the single-owner and no-exposure rules."""

    def test_target_revision_defaults_to_main(self):
        assert build_config().argocd_repo_target_revision == "main"

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
