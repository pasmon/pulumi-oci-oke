"""Pulumi program that provisions an OKE cluster on two free-tier ARM nodes.

One ``pulumi up`` takes you from nothing to a working OKE cluster with a
self-driving Argo CD. Pulumi creates only things that need OCI or must exist
before GitOps can run. Everything that depends on a CRD installed by Argo CD
lives in ``argo-apps``.

No application secret is created by this program. Credentials live in OCI Vault
and reach the cluster through External Secrets Operator.
"""

import pulumi

from oke import (
    argocd,
    cluster,
    identity,
    kubeconfig,
    namespaces,
    networking,
    wireguard,
)
from oke.config import Config

cfg = Config()

# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #
network = networking.create_network(cfg)

# --------------------------------------------------------------------------- #
# Cluster
# --------------------------------------------------------------------------- #
oke = cluster.create_cluster(
    cfg,
    vcn_id=network["vcn"].id,
    endpoint_subnet_id=network["endpoint_subnet"].id,
    nodes_subnet_id=network["nodes_subnet"].id,
    service_subnet_id=network["service_subnet"].id,
    node_metadata=wireguard.build_node_user_data(cfg),
)

# --------------------------------------------------------------------------- #
# Vault access for External Secrets Operator
# --------------------------------------------------------------------------- #
# The dynamic group matches the exact instance OCIDs reported by this node
# pool. The policy is needed before GitOps starts ESO and its ClusterSecretStore.
vault_read_policy = identity.create_vault_read_policy(cfg, oke["node_pool"])

# --------------------------------------------------------------------------- #
# Kubeconfig
# --------------------------------------------------------------------------- #
# OKE returns a kubeconfig already pointed at the public endpoint, so unlike the
# self-managed RKE2 setup there is no server address to rewrite.
admin_kubeconfig = kubeconfig.get_kubeconfig(oke["cluster"].id)
admin_kubeconfig.content.apply(kubeconfig.write_kubeconfig)

# --------------------------------------------------------------------------- #
# Namespaces that GitOps writes into
# --------------------------------------------------------------------------- #
# Created here rather than by Argo CD so the External Secrets targets always
# have somewhere to land, whichever Application syncs first.
created_namespaces = namespaces.create_namespaces(
    admin_kubeconfig.content,
    [
        namespaces.CERT_MANAGER_NAMESPACE,
        namespaces.MONITORING_NAMESPACE,
        # The ESO chart creates its own namespace with CreateNamespace, so this
        # is not strictly needed. It stays because the ClusterSecretStore and the
        # ExternalSecrets it feeds have to resolve the same way whichever
        # Application syncs first.
        namespaces.ESO_NAMESPACE,
    ],
)

# --------------------------------------------------------------------------- #
# Argo CD
# --------------------------------------------------------------------------- #
argocd_resources = argocd.create_argocd(
    cfg,
    admin_kubeconfig.content,
    bootstrap_dependencies=[vault_read_policy] if vault_read_policy else None,
)

# --------------------------------------------------------------------------- #
# Exports
# --------------------------------------------------------------------------- #
pulumi.export("cluster_id", oke["cluster"].id)
pulumi.export("cluster_name", oke["cluster"].name)
pulumi.export(
    "kubernetes_endpoint",
    oke["cluster"].endpoint_config.apply(lambda config: config.is_public_ip_enabled),
)
pulumi.export("kubeconfig_path", kubeconfig.KUBECONFIG_PATH)

pulumi.export("vcn_cidr", cfg.vcn_cidr)
pulumi.export("endpoint_subnet_id", network["endpoint_subnet"].id)
pulumi.export("nodes_subnet_id", network["nodes_subnet"].id)
pulumi.export("service_subnet_id", network["service_subnet"].id)

pulumi.export("kubernetes_version", oke["kubernetes_version"])
pulumi.export("node_image_id", oke["node_image_id"])
pulumi.export("availability_domains", oke["availability_domains"])
pulumi.export("pods_cidr", cfg.pods_cidr)
pulumi.export("services_cidr", cfg.services_cidr)

pulumi.export("node_count", cfg.node_count)
pulumi.export("node_ocpus", cfg.node_ocpus)
pulumi.export("node_memory_gbs", cfg.node_memory_gbs)
pulumi.export("node_total_ocpus", cfg.node_total_ocpus)
pulumi.export("node_total_memory_gbs", cfg.node_total_memory_gbs)

pulumi.export("argocd_namespace", argocd.ARGOCD_NAMESPACE)
pulumi.export("argocd_managed_by_pulumi", cfg.argocd_managed_by_pulumi)
pulumi.export("argocd_bootstrap_application", "bootstrap-root")
# The path Pulumi seeds, so the operator can confirm the hand-over target
# without reading the program.
pulumi.export("argocd_bootstrap_path", cfg.argocd_repo_path)
pulumi.export("argocd_bootstrap_repo_url", cfg.argocd_repo_url)

pulumi.export("created_namespaces", sorted(created_namespaces.keys()))

# Vault access, so the README's verification step can be checked without reading
# the program's output by other means.
pulumi.export(
    "vault_read_policy_id", vault_read_policy.id if vault_read_policy else None
)
pulumi.export("vault_id", cfg.vault_id)

pulumi.export("wireguard_enabled", cfg.wireguard_enabled)
if cfg.wireguard_enabled:
    pulumi.export("wireguard_subnet_cidr", cfg.wireguard_subnet_cidr)
    pulumi.export("wireguard_listen_port", cfg.wireguard_listen_port)
    pulumi.export("wireguard_router_address", cfg.wireguard_router_address)
    pulumi.export("wireguard_allowed_ips", wireguard.wireguard_allowed_ips(cfg))
