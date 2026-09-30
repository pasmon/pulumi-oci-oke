"""OKE Workload Identity prerequisites.

External Secrets Operator authenticates to OCI Vault using Workload Identity,
which needs the cluster's ``OKE`` dynamic group to hold a dynamic policy and a
policy granting ``read`` on the ``any-user`` dynamic group.

A Workload Identity policy assignment cannot be performed by a user principal,
only by a dynamic group, so the console step in the README is unavoidable. This
module creates the dynamic group and exports everything needed to complete it.
"""

import pulumi_oci as oci

# The node-level dynamic group that OKE uses to identify cluster workloads. Its
# exact OCID is tenancy specific, which is why it is configuration.
OKE_DYNAMIC_GROUP_NAME = "oke"

# Annotation prefix OCI uses to bind a Kubernetes ServiceAccount to a dynamic
# group. The full annotation key continues with the dynamic group OCID, which is
# only known after the dynamic group is attached, so it is exported for the
# operator to paste into gitops/bootstrap/cluster-external-secrets.yaml.
DYNAMIC_GROUP_ANNOTATION_PREFIX = "identity.oci.authorization.oke.info/"


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
