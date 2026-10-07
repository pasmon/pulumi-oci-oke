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
# group. The key continues with the dynamic group's *name*, not its OCID: an
# OCID is 84 bytes and Kubernetes caps an annotation's name part at 63, so an
# OCID key is rejected by the API server before OKE ever reads it.
DYNAMIC_GROUP_ANNOTATION_PREFIX = "identity.oci.authorization.oke.info/"

# The group's name, which is what the ServiceAccount annotation names. A
# constant rather than the resource's `name` Output, because an annotation key
# cannot hold an Output and this program already knows the name.
OKE_DYNAMIC_GROUP_FULL_NAME = f"{OKE_DYNAMIC_GROUP_NAME}-radio"

# Kubernetes annotation name-part limit, in bytes.
ANNOTATION_NAME_MAX_BYTES = 63


def spiffe_id(cluster_id, namespace, name):
    """Return the SPIFFE ID that OKE issues a workload identity token for.

    OKE derives the token's subject from the cluster OCID and the
    ServiceAccount's coordinates, and the matching dynamic group rule is written
    against exactly this string. Getting the two out of step fails
    authentication rather than deployment, which is why it is built in one
    place and used by both sides.
    """
    return f"spiffe://{cluster_id}/ns/{namespace}/sa/{name}"


def workload_identity_annotations(dynamic_group, cluster_id, namespace, name):
    """Return the annotation binding one ServiceAccount to the dynamic group.

    The annotation key names the dynamic group and the value is the SPIFFE ID.
    Only the SPIFFE ID depends on an Output, so the map is built inside an
    apply at the call site.

    Args:
        dynamic_group: the group name, which fits in an annotation key.
        cluster_id: the cluster OCID, which is part of the SPIFFE ID.
        namespace: the ServiceAccount's namespace.
        name: the ServiceAccount's name.

    Raises:
        ValueError: if the group name cannot fit in an annotation key.
    """
    if len(dynamic_group.encode("utf-8")) > ANNOTATION_NAME_MAX_BYTES:
        raise ValueError(
            f"dynamic group name {dynamic_group!r} exceeds the "
            f"{ANNOTATION_NAME_MAX_BYTES}-byte annotation key limit"
        )

    return {
        f"{DYNAMIC_GROUP_ANNOTATION_PREFIX}{dynamic_group}": spiffe_id(
            cluster_id, namespace, name
        )
    }


def oke_dynamic_group_rule(tenancy_id):
    """Build the matching rule for the OKE node dynamic group.

    Every OCI node instance principal carries the tenancy's user OCID, so a
    single ``any.user.tenantid`` equality matches all of them.

    The comparison is a single ``=``. IAM matching rules take ``=`` or ``!=``
    only, and IDCS rejects ``==`` as an unparseable rule, which fails the
    create rather than the policy.
    """
    return f"ALL {{any.user.tenantid = '{tenancy_id}'}}"


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
        name=OKE_DYNAMIC_GROUP_FULL_NAME,
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
            "annotations": cluster_id.apply(
                lambda resolved: workload_identity_annotations(
                    OKE_DYNAMIC_GROUP_FULL_NAME, resolved, namespace, name
                )
            ),
        },
        # The annotation names the dynamic group, so this cannot be created
        # before the group exists.
        opts=pulumi.ResourceOptions(provider=provider, depends_on=[dynamic_group]),
    )
