"""Pulumi program that provisions an OKE cluster on two free-tier ARM nodes.

One ``pulumi up`` takes you from nothing to a working OKE cluster with a
self-driving Argo CD. Pulumi creates only things that need OCI or must exist
before GitOps can run. Everything that depends on a CRD installed by Argo CD
lives in ``argo-apps``.

No application secret is created by this program. Credentials live in OCI Vault
and reach the cluster through External Secrets Operator.
"""

import pulumi

from oke import argocd, cluster, identity, kubeconfig, namespaces, networking, wireguard
from oke.config import Config

cfg = Config()

# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #
network = networking.create_network(cfg)

# --------------------------------------------------------------------------- #
# Workload Identity prerequisite
# --------------------------------------------------------------------------- #
# IAM resources live in the tenancy root. This dynamic group is what lets OKE
# workloads assume a dynamic policy, which is how ESO authenticates to Vault
# without any static credential.
oke_dynamic_group = identity.create_oke_dynamic_group(cfg)

# --------------------------------------------------------------------------- #
# Cluster
# --------------------------------------------------------------------------- #
oke = cluster.create_cluster(
    cfg,
    vcn_id=network["vcn"].id,
    endpoint_subnet_id=network["endpoint_subnet"].id,
    nodes_subnet_id=network["nodes_subnet"].id,
    node_metadata=wireguard.build_node_user_data(cfg),
)

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
        # Needed before the ServiceAccount below, which cannot be created in a
        # namespace that does not exist.
        namespaces.ESO_NAMESPACE,
    ],
)

# --------------------------------------------------------------------------- #
# External Secrets ServiceAccount
# --------------------------------------------------------------------------- #
# The Workload Identity annotation needs two OCIDs this program produces: the
# dynamic group's and the cluster's. Templating the ServiceAccount here is what
# removes the hand-edited manifest from the bootstrap flow, leaving the OCI
# console policy attachment as the only manual step.
eso_service_account = identity.create_eso_service_account(
    cfg,
    admin_kubeconfig.content,
    cluster_id=oke["cluster"].id,
    dynamic_group=oke_dynamic_group,
)

# --------------------------------------------------------------------------- #
# Argo CD
# --------------------------------------------------------------------------- #
argocd_resources = argocd.create_argocd(cfg, admin_kubeconfig.content)

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

# Workload Identity. The identifiers are exported so the console step in the
# README can be completed without reading the program's output by other means.
pulumi.export(
    "oke_dynamic_group_id", oke_dynamic_group.id if oke_dynamic_group else None
)
pulumi.export("eso_service_account_namespace", cfg.eso_service_account_namespace)
pulumi.export("eso_service_account_name", cfg.eso_service_account_name)
pulumi.export("eso_service_account_created", eso_service_account is not None)

pulumi.export("wireguard_enabled", cfg.wireguard_enabled)
if cfg.wireguard_enabled:
    pulumi.export("wireguard_subnet_cidr", cfg.wireguard_subnet_cidr)
    pulumi.export("wireguard_listen_port", cfg.wireguard_listen_port)
    pulumi.export("wireguard_router_address", cfg.wireguard_router_address)
    pulumi.export("wireguard_allowed_ips", wireguard.wireguard_allowed_ips(cfg))
