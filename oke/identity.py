"""OKE Workload Identity prerequisites.

External Secrets Operator authenticates to OCI Vault using Workload Identity,
which needs the cluster's ``OKE`` dynamic group to hold a dynamic policy and a
policy granting ``read`` on the ``any-user`` dynamic group.

A Workload Identity policy assignment cannot be performed by a user principal,
only by a dynamic group, so the console step in the README is unavoidable. This
module creates everything on the Kubernetes side of that binding, including the
ServiceAccount and its annotation, because both values the annotation needs are
already known here: the dynamic group OCID and the cluster OCID.

That is the reason this is not a manifest in the GitOps repository. Both OCIDs
are outputs of this program, so writing the annotation by hand would mean
copying two values out of ``pulumi stack output`` into a file that then has to
be committed before the cluster can authenticate. Templating it here removes
that step entirely, and leaves the console as the single manual action.
"""

import pulumi
import pulumi_kubernetes as k8s
import pulumi_oci as oci

# The node-level dynamic group that OKE uses to identify cluster workloads. Its
# exact OCID is tenancy specific, which is why it is configuration.
OKE_DYNAMIC_GROUP_NAME = "oke"

# Annotation prefix OCI uses to bind a Kubernetes ServiceAccount to a dynamic
# group. The full annotation key continues with the dynamic group OCID, which is
# an output of create_oke_dynamic_group rather than a config value.
DYNAMIC_GROUP_ANNOTATION_PREFIX = "identity.oci.authorization.oke.info/"


def spiffe_id(cluster_id, namespace, name):
    """Return the SPIFFE ID that OKE issues a workload identity token for.

    OKE derives the token's subject from the cluster OCID and the
    ServiceAccount's coordinates, and the matching dynamic group rule is written
    against exactly this string. Getting the two out of step fails
    authentication rather than deployment, which is why it is built in one
    place and used by both sides.
    """
    return f"spiffe://{cluster_id}/ns/{namespace}/sa/{name}"


def workload_identity_annotations(dynamic_group_id, cluster_id, namespace, name):
    """Return the annotation binding one ServiceAccount to the dynamic group.

    The annotation key is the prefix plus the dynamic group OCID, and the value
    is the SPIFFE ID. Both are Outputs, which the Kubernetes provider resolves
    during apply, so the resulting object is correct without a second pass.
    """
    return {
        f"{DYNAMIC_GROUP_ANNOTATION_PREFIX}{dynamic_group_id}": spiffe_id(
            cluster_id, namespace, name
        )
    }


def oke_dynamic_group_rule(tenancy_id):
    """Build the matching rule for the OKE node dynamic group.

    Every OKE node instance principal carries the tenancy's user OCID, so a
    single ``any.user.tenantid`` equality matches all of them.
    """
    return f"ALL {{any.user.tenantid == '{tenancy_id}'}}"


def create_oke_dynamic_group(cfg):
    """Create the dynamic group that lets OKE workloads assume dynamic policies.

    Args:
        cfg: the validated :class:`oke.config.Config`.

    Returns:
        The ``oci.identity.DynamicGroup`` resource, or ``None`` when
        ``tenancy-id`` is not configured.
    """
    if cfg.tenancy_id is None:
        return None

    return oci.identity.DynamicGroup(
        "oke-cluster-dynamic-group",
        # IAM resources live in the tenancy root, which is the compartment here.
        compartment_id=cfg.tenancy_id,
        name=f"{OKE_DYNAMIC_GROUP_NAME}-radio",
        description="OKE cluster service accounts for workload identity",
        matching_rule=oke_dynamic_group_rule(cfg.tenancy_id),
    )


def create_eso_service_account(cfg, kubeconfig, cluster_id, dynamic_group):
    """Create the External Secrets ServiceAccount with its identity annotation.

    External Secrets Operator's ClusterSecretStore in the GitOps repository
    references this ServiceAccount by name. Pulumi owns the object so that the
    two OCIDs in the annotation are templated rather than transcribed.

    Args:
        cfg: the validated :class:`oke.config.Config`.
        kubeconfig: the OKE kubeconfig, used to build the Kubernetes provider.
        cluster_id: the OKE cluster OCID, which is part of the SPIFFE ID.
        dynamic_group: the DynamicGroup resource, or ``None`` when ``tenancy-id``
            is unset.

    Returns:
        The ServiceAccount resource, or ``None`` when Workload Identity is not
        configured.
    """
    if dynamic_group is None:
        return None

    provider = k8s.Provider(
        "oke-eso-service-account",
        kubeconfig=kubeconfig,
        enable_server_side_apply=True,
    )

    namespace = cfg.eso_service_account_namespace
    name = cfg.eso_service_account_name

    return k8s.core.v1.ServiceAccount(
        "eso-service-account",
        metadata={
            "name": name,
            "namespace": namespace,
            "annotations": workload_identity_annotations(
                dynamic_group.id, cluster_id, namespace, name
            ),
        },
        # The annotation carries the dynamic group OCID, so this cannot be
        # created before the group exists.
        opts=pulumi.ResourceOptions(provider=provider, depends_on=[dynamic_group]),
    )
