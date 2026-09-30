"""Namespaces that must exist before GitOps can write into them.

External Secrets Operator syncs credentials into ``cert-manager`` and
``monitoring``. Those namespaces are created here, ahead of Argo CD, so the
Secret targets always have somewhere to land.

**No Secret is ever created by this module.** Credentials live in OCI Vault and
reach the cluster only through ESO. The single exception in the whole program is
the optional Argo CD repository credential in :mod:`oke.argocd`, which is
inherent to bootstrapping GitOps from a private remote.
"""

import pulumi
import pulumi_kubernetes as k8s

# External Secrets Operator syncs the Cloudflare dns-01 API token into
# cert-manager, which reads apiTokenSecretRef from its cluster resource
# namespace. Argo CD's cert-manager chart uses CreateNamespace, so creating it
# early is a no-op for that Application and Argo CD will not prune it.
CERT_MANAGER_NAMESPACE = "cert-manager"

# External Secrets Operator syncs the Grafana Cloud destination credentials
# here, and the k8s-monitoring chart references the Secret by this name.
MONITORING_NAMESPACE = "monitoring"

# The namespace argo-apps deploys the radio workloads into.
RADIO_NAMESPACE = "radio"


def create_namespaces(kubeconfig, namespaces):
    """Create the namespaces that GitOps writes into.

    Args:
        kubeconfig: the OKE kubeconfig, used to build the provider.
        namespaces: names of the namespaces to create.

    Returns:
        A dict mapping namespace name to the created Namespace resource.
    """
    provider = k8s.Provider(
        "oke-namespaces",
        kubeconfig=kubeconfig,
        enable_server_side_apply=True,
    )

    created = {}
    for name in namespaces:
        created[name] = k8s.core.v1.Namespace(
            f"namespace-{name}",
            metadata={"name": name},
            opts=pulumi.ResourceOptions(provider=provider),
        )

    return created
