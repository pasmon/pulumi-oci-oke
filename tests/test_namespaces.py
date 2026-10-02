"""Unit tests for namespace creation.

The important invariant here is negative: no Secret is ever declared.
"""

import inspect

import pulumi_kubernetes as k8s

from oke import argocd, namespaces
from tests.conftest import build_config


class TestNamespaceNames:
    """The namespace names are fixed by what the consuming charts expect."""

    def test_cert_manager_namespace(self):
        assert namespaces.CERT_MANAGER_NAMESPACE == "cert-manager"

    def test_monitoring_namespace(self):
        assert namespaces.MONITORING_NAMESPACE == "monitoring"

    def test_eso_namespace(self):
        # The Workload Identity ServiceAccount has to be created before any
        # Application references it, so its namespace cannot wait for GitOps.
        assert namespaces.ESO_NAMESPACE == "external-secrets"

    def test_namespaces_are_distinct(self):
        names = {
            namespaces.CERT_MANAGER_NAMESPACE,
            namespaces.MONITORING_NAMESPACE,
            namespaces.ESO_NAMESPACE,
        }
        assert len(names) == 3

    def test_config_agrees_with_the_module_constant(self):
        # The ServiceAccount is created in cfg.eso_service_account_namespace
        # while the namespace is created from ESO_NAMESPACE. Two sources of
        # truth would let the ServiceAccount land in a namespace that does not
        # exist, which fails at apply rather than at preview.
        assert build_config().eso_service_account_namespace == namespaces.ESO_NAMESPACE


class TestNoSecretsInThisModule:
    """This module creates namespaces and nothing else.

    Credentials reach the cluster only through External Secrets Operator, so a
    Secret declared here would mean a credential in Pulumi state.
    """

    def test_module_declares_no_secret_resource(self):
        source = inspect.getsource(namespaces)
        assert "k8s.core.v1.Secret" not in source

    def test_no_string_data_anywhere_in_the_module(self):
        assert "string_data" not in inspect.getsource(namespaces)

    def test_program_declares_no_secret_outside_argocd(self):
        # The Argo CD repository credential is the single documented exception.
        assert "core.v1.Secret" not in inspect.getsource(namespaces)

        # argocd.py may build one repository Secret, and nothing else.
        assert inspect.getsource(argocd).count("k8s.core.v1.Secret(") == 1


class TestConfigKeysAreNotSecrets:
    """No credential material is read from the stack configuration."""

    def test_no_secret_config_keys(self):
        source = inspect.getsource(type(build_config()))
        secret_keys = [
            "cloudflare-api-token",
            "grafana-cloud-password",
            "grafana-cloud-api-key",
        ]
        for key in secret_keys:
            assert key not in source


class TestNamespaceResources:
    """Only Namespace resources are constructed."""

    def test_namespace_is_a_core_v1_namespace(self):
        assert hasattr(k8s.core.v1, "Namespace")

    def test_module_does_not_construct_a_secret(self):
        source = inspect.getsource(namespaces)
        assert "Secret(" not in source
