"""Engine-mocked tests that load __main__.py end to end.

These catch wiring mistakes the unit tests cannot: a wrong argument name on a
Pulumi resource, a missing dependency, or a data source whose mocked shape does
not match how the program reads it. Nothing is created against OCI.
"""

import asyncio
import base64
import inspect
import os

import pulumi
import pytest

from oke import argocd, kubeconfig, wireguard
from tests.conftest import (
    BASE_STACK_CONFIG,
    REPO_ROOT,
    build_config,
    load_stack,
    unload_stack,
)

# A validated config for the pure-function assertions. No Pulumi engine and no
# OCI access, so these run even where the mocked stack cannot be loaded.
BASE_CONFIG = build_config()


class TestProgramLoads:
    """The program runs end to end under mocks."""

    def test_cluster_is_created(self, pulumi_stack):
        assert pulumi_stack.oke["cluster"] is not None

    def test_node_pool_is_created(self, pulumi_stack):
        assert pulumi_stack.oke["node_pool"] is not None

    def test_network_resources_exist(self, pulumi_stack):
        network = pulumi_stack.network
        for key in (
            "vcn",
            "internet_gateway",
            "route_table",
            "endpoint_subnet",
            "nodes_subnet",
            "service_subnet",
        ):
            assert network[key] is not None, f"missing {key}"

    def test_load_balancer_subnet_is_not_the_node_subnet(self, pulumi_stack):
        # OKE rejects a node pool placed in a service load balancer subnet, so
        # these two have to be different subnets.
        network = pulumi_stack.network
        assert network["service_subnet"] is not network["nodes_subnet"]

    def test_the_three_namespaces_are_created(self, pulumi_stack):
        assert set(pulumi_stack.created_namespaces) == {
            "cert-manager",
            "monitoring",
            "external-secrets",
        }

    def test_kubeconfig_is_written(self, tmp_path, monkeypatch):
        # The .apply() callback only runs when the output resolves, which the
        # engine does outside the import. Exercise the writer directly.
        monkeypatch.chdir(tmp_path)
        written = kubeconfig.write_kubeconfig(
            "apiVersion: v1\nclusters: []\nserver: https://k8s.eu-stockholm-1.oke.test\n"
        )
        assert os.path.exists(tmp_path / "out" / "oke_kubeconfig")
        assert written is not None

    def test_kubeconfig_content_is_not_rewritten(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        original = "server: https://k8s.eu-stockholm-1.oke.test\n"
        kubeconfig.write_kubeconfig(original)
        with open(tmp_path / "out" / "oke_kubeconfig", encoding="utf-8") as handle:
            assert handle.read() == original

    @pytest.mark.skipif(
        os.name == "nt", reason="POSIX permission bits are not reported on Windows"
    )
    def test_kubeconfig_is_written_with_owner_only_permissions(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        kubeconfig.write_kubeconfig("apiVersion: v1\n")
        mode = os.stat(tmp_path / "out" / "oke_kubeconfig").st_mode
        # 0o600: the file holds a cluster-admin credential.
        assert mode & 0o077 == 0

    def test_kubeconfig_mode_constant_is_owner_only(self):
        assert kubeconfig.KUBECONFIG_MODE == 0o600

    def test_kubeconfig_none_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert kubeconfig.write_kubeconfig(None) is None
        assert not (tmp_path / "out" / "oke_kubeconfig").exists()


class TestArgoCdExposure:
    """Argo CD is bootstrapped but never exposed.

    Pulumi's mocks only register resources once an Output resolves, which does
    not happen during import. These tests assert on the values the program
    computes and on the gitops manifests it ships, rather than on resource
    registrations that a mock run never performs.
    """

    def test_release_is_created(self, pulumi_stack):
        assert pulumi_stack.argocd_resources["release"] is not None

    def test_bootstrap_application_is_created(self, pulumi_stack):
        assert pulumi_stack.argocd_resources["bootstrap_application"] is not None

    def test_chart_coordinates_are_pinned(self, pulumi_stack):
        assert argocd.ARGOCD_HELM_CHART == "argo-cd"
        assert argocd.ARGOCD_HELM_VERSION == "8.3.3"
        assert argocd.ARGOCD_NAMESPACE == "argocd"
        # Loading the stack at all proves the pinned coordinates are accepted.
        assert pulumi_stack.argocd_resources is not None

    def test_no_gateway_key_is_returned(self, pulumi_stack):
        # Public ingress belongs to argo-apps, which owns the Gateway CRDs.
        assert "gateway" not in pulumi_stack.argocd_resources

    def test_program_module_declares_no_gateway_resource(self, pulumi_stack):
        source = inspect.getsource(pulumi_stack.argocd)
        assert "Gateway" not in source
        assert "HTTPRoute" not in source


class TestGitopsHandover:
    """Pulumi's entire Argo CD footprint is one Application.

    The bootstrap is minimal by construction, so these assert on the absence of
    things rather than the presence of a manifest tree. A reintroduced
    intermediate Application or a second repository entrypoint is exactly the
    regression this guards against.
    """

    def test_no_gitops_manifest_tree_ships(self):
        # Everything after the release lives in argo-apps, so there is no
        # bootstrap directory to drift out of step with it.
        assert not os.path.exists(os.path.join(REPO_ROOT, "gitops"))

    def test_source_is_a_recursive_directory(self):
        # A single file here would hardcode one category instead of letting
        # argo-apps declare its own entrypoint.
        spec = argocd.bootstrap_application_spec(BASE_CONFIG)
        assert spec["source"]["directory"] == {"recurse": True}

    def test_uses_the_default_project(self):
        # `bootstrap` cannot be used: it would have to be created by the very
        # Application that references it. Argo CD's own default project is
        # created permissive, so it is the only one available at this point.
        spec = argocd.bootstrap_application_spec(BASE_CONFIG)
        assert spec["project"] == "default"

    def test_handover_is_automated(self):
        spec = argocd.bootstrap_application_spec(BASE_CONFIG)
        assert spec["syncPolicy"]["automated"] == {"prune": True, "selfHeal": True}

    def test_destination_is_the_argocd_namespace(self):
        spec = argocd.bootstrap_application_spec(BASE_CONFIG)
        assert spec["destination"]["namespace"] == argocd.ARGOCD_NAMESPACE


class TestHandoffFlag:
    """Disabling the flag removes the release so Argo CD can adopt it."""

    def test_release_is_absent_when_not_managed_by_pulumi(self):
        program = load_stack({**BASE_STACK_CONFIG, "argocd-managed-by-pulumi": "false"})
        try:
            assert program.argocd_resources["release"] is None
            # The bootstrap Application still exists so Argo CD can take over.
            assert program.argocd_resources["bootstrap_application"] is not None
        finally:
            unload_stack(program)


class TestVaultAccess:
    """The vault read policy appears only with both OCIDs configured."""

    def test_absent_by_default(self, pulumi_stack):
        assert pulumi_stack.vault_read_policy is None

    def test_created_with_a_tenancy_and_vault(self, identity_stack):
        assert identity_stack.vault_read_policy is not None

    def test_created_before_the_argo_cd_bootstrap(self, identity_stack):
        # ESO cannot sync anything until the policy exists, so it has to be
        # wired before the hand-over rather than after GitOps takes over.
        assert identity_stack.vault_read_policy is not None
        assert identity_stack.argocd_resources["bootstrap_application"] is not None


class TestNodeMetadata:
    """Wireguard user data reaches the node pool only when configured."""

    def test_no_metadata_by_default(self, pulumi_stack):
        assert wireguard_user_data(pulumi_stack) is None

    def test_metadata_present_when_wireguard_is_configured(self, wireguard_stack):
        payload = wireguard_user_data(wireguard_stack)
        assert payload is not None
        assert "wg-quick@wg0" in decode(payload)

    def test_wireguard_export_is_true_when_configured(self, wireguard_stack):
        assert wireguard_stack.cfg.wireguard_enabled is True

    def test_wireguard_export_is_false_by_default(self, pulumi_stack):
        assert pulumi_stack.cfg.wireguard_enabled is False


def wireguard_user_data(program):
    """Read the node_metadata that was passed to the node pool.

    The secret keys are Outputs, so the payload is one too and has to be
    resolved before it can be read.
    """
    payload = wireguard.build_node_user_data(program.cfg)
    if payload is None or not isinstance(payload, pulumi.Output):
        return payload
    # The loop load_stack installed, not a new one: the Output's futures are
    # already attached to it and a fresh loop deadlocks.
    return asyncio.get_event_loop().run_until_complete(payload.future())


def decode(payload):
    """Base64-decode a node user data payload."""
    return base64.b64decode(payload).decode("utf-8")
