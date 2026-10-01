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
    [namespaces.CERT_MANAGER_NAMESPACE, namespaces.MONITORING_NAMESPACE],
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

pulumi.export("created_namespaces", sorted(created_namespaces.keys()))

# Workload Identity enrollment inputs. These are identifiers, not credentials.
pulumi.export(
    "oke_dynamic_group_id", oke_dynamic_group.id if oke_dynamic_group else None
)
pulumi.export("eso_service_account_namespace", cfg.eso_service_account_namespace)
pulumi.export("eso_service_account_name", cfg.eso_service_account_name)
pulumi.export(
    "eso_dynamic_group_annotation_prefix", identity.DYNAMIC_GROUP_ANNOTATION_PREFIX
)

pulumi.export("wireguard_enabled", cfg.wireguard_enabled)
if cfg.wireguard_enabled:
    pulumi.export("wireguard_subnet_cidr", cfg.wireguard_subnet_cidr)
    pulumi.export("wireguard_listen_port", cfg.wireguard_listen_port)
    pulumi.export("wireguard_router_address", cfg.wireguard_router_address)
    pulumi.export("wireguard_allowed_ips", wireguard.wireguard_allowed_ips(cfg))
