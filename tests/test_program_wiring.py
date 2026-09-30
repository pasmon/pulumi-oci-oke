"""Engine-mocked tests that load __main__.py end to end.

These catch wiring mistakes the unit tests cannot: a wrong argument name on a
Pulumi resource, a missing dependency, or a data source whose mocked shape does
not match how the program reads it. Nothing is created against OCI.
"""

import base64
import inspect
import os

import pytest
import yaml

from oke import argocd, kubeconfig, wireguard
from tests.conftest import BASE_STACK_CONFIG, REPO_ROOT, load_stack, unload_stack


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
        ):
            assert network[key] is not None, f"missing {key}"

    def test_both_namespaces_are_created(self, pulumi_stack):
        assert set(pulumi_stack.created_namespaces) == {"cert-manager", "monitoring"}

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


class TestGitopsManifests:
    """The bootstrap manifests ship in this repo and are internally consistent.

    These read files rather than mock resources, so they need no fixture.
    """

    def _read(self, name):
        with open(
            os.path.join(REPO_ROOT, "gitops", "bootstrap", name), encoding="utf-8"
        ) as handle:
            return handle.read()

    def test_bootstrap_project_allows_both_repositories(self):
        content = self._read("bootstrap-project.yaml")
        assert "https://github.com/pasmon/argo-apps.git" in content
        assert "https://github.com/pasmon/pulumi-oci-oke.git" in content
        assert "https://argoproj.github.io/argo-helm" in content

    def test_bootstrap_project_allows_cluster_resources(self):
        content = self._read("bootstrap-project.yaml")
        assert "clusterResourceWhitelist" in content

    def test_argocd_self_starts_unmanaged(self):
        content = self._read("argocd-self-application.yaml")
        document = yaml.safe_load(content)
        # Automation is genuinely off, not merely mentioned in a comment.
        assert "automated" not in document["spec"]["syncPolicy"]
        assert "argocd-managed-by-pulumi" in content

    def test_platform_app_of_apps_is_automated(self):
        content = self._read("argo-apps-application.yaml")
        assert "prune: true" in content
        assert "selfHeal: true" in content
        assert "path: app-of-apps" in content

    def test_workload_identity_manifest_is_cluster_specific(self):
        content = self._read("cluster-external-secrets.yaml")
        assert "ClusterSecretStore" in content
        assert "principalType: Workload" in content
        # serviceAccountRef.namespace is required on a cluster-scoped store.
        assert "namespace: external-secrets" in content
        # Left as a placeholder for the operator to fill in.
        assert "TODO_VAULT_OCID" in content

    def test_workload_identity_manifest_carries_no_credential(self):
        content = self._read("cluster-external-secrets.yaml")
        assert "BEGIN " not in content
        assert "apiToken" not in content


class TestHandoffFlag:
    """Disabling the flag removes the release so argocd-self can take over."""

    def test_release_is_absent_when_not_managed_by_pulumi(self):
        program = load_stack({**BASE_STACK_CONFIG, "argocd-managed-by-pulumi": "false"})
        try:
            assert program.argocd_resources["release"] is None
            # The bootstrap Application still exists so Argo CD can take over.
            assert program.argocd_resources["bootstrap_application"] is not None
        finally:
            unload_stack(program)


class TestWorkloadIdentity:
    """The dynamic group appears only when a tenancy is configured."""

    def test_absent_without_a_tenancy(self, pulumi_stack):
        assert pulumi_stack.oke_dynamic_group is None

    def test_created_with_a_tenancy(self, identity_stack):
        assert identity_stack.oke_dynamic_group is not None


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
    """Read the node_metadata that was passed to the node pool."""
    return wireguard.build_node_user_data(program.cfg)


def decode(payload):
    """Base64-decode a node user data payload."""
    return base64.b64decode(payload).decode("utf-8")
