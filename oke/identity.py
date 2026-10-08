"""The IAM identity and policy that let cluster nodes read OCI Vault.

External Secrets Operator authenticates to OCI Vault as an *instance principal*:
the identity of the compute instance the pod happens to run on. This program
creates a dynamic group that matches only the compute instances in this node
pool, then grants that group read access to one vault.

Workload Identity would be finer grained, but OKE only offers it on *enhanced*
clusters, which are billed at $0.10/cluster-hour. This program creates a basic
cluster on purpose to stay inside Always Free, so instance principals are the
only credential-free option available. The cost of that choice is scope: any
pod on a node can read the vault, not just ESO. The policy is therefore
restricted to one vault and to ``read``, which is the smallest grant that works.
"""

import pulumi
import pulumi_oci as oci

OKE_NODE_POOL_DYNAMIC_GROUP_NAME = "radio-oke-node-pool-instances"

# The read permission ESO needs on a vault. OCI exposes a vault's secrets through
# the ``secret-family`` resource rather than ``secret``; the former covers
# listing, which ESO does not need but the granularity costs nothing.
VAULT_READ_PERMISSION = "secret-family"


def statement_scope(compartment_id, tenancy_id):
    """Render the ``in ...`` clause for the compartment holding the vault.

    OCI rejects a tenancy OCID on the left of a policy statement: CreatePolicy
    answers 400 ``Compartment {...} does not exist or is not part of the policy
    compartment subtree``. The tenancy root is not addressable as a compartment,
    it is spelled ``tenancy``. Deploying into the root compartment is normal
    here, since ``compartment-id`` is set to the tenancy OCID.

    Args:
        compartment_id: the compartment holding the vault.
        tenancy_id: the tenancy root OCID, or ``None`` if not configured.

    Returns:
        Either ``tenancy`` or ``compartment <ocid>``.
    """
    if compartment_id == tenancy_id:
        return "tenancy"
    return f"compartment {compartment_id}"


def node_pool_matching_rule(nodes):
    """Build a dynamic-group rule from the node pool's exact instance OCIDs."""
    instance_ids = [node.get("id") for node in nodes if node.get("id")]
    if not nodes or len(instance_ids) != len(nodes):
        raise ValueError("OKE node pool did not return an instance OCID for every node")
    if any(
        not instance_id.startswith("ocid1.instance.") for instance_id in instance_ids
    ):
        raise ValueError("OKE node pool returned a non-instance OCID")

    conditions = ", ".join(
        f"instance.id = '{instance_id}'" for instance_id in sorted(set(instance_ids))
    )
    return f"ANY {{{conditions}}}"


def vault_read_statement(compartment_id, vault_id, tenancy_id=None):
    """Build the IAM statement granting node principals read on one vault.

    The principal is a compute instance rather than a pod, so the statement
    names ``request.principal.type = 'instance'`` rather than any Kubernetes
    coordinates. The dedicated dynamic group limits the instance principals to
    this node pool; ``target.vault.id`` limits access to one vault.

    Args:
        compartment_id: the compartment holding the vault.
        vault_id: the vault OCID.
        tenancy_id: the tenancy root OCID, used to detect the root compartment.

    Returns:
        The statement, ready for ``oci.identity.Policy``.
    """
    return (
        f"Allow dynamic-group {OKE_NODE_POOL_DYNAMIC_GROUP_NAME} "
        f"to read {VAULT_READ_PERMISSION} "
        f"in {statement_scope(compartment_id, tenancy_id)} where all {{"
        f"request.principal.type = 'instance', "
        f"target.vault.id = '{vault_id}'"
        f"}}"
    )


def create_vault_read_policy(cfg, node_pool):
    """Create the read-only vault policy for the cluster's node principals.

    Args:
        cfg: the validated :class:`oke.config.Config`.
        node_pool: the OKE node pool whose instances may read the vault.

    Returns:
        The ``oci.identity.Policy`` resource, or ``None`` when ``vault-id`` is
        not configured.
    """
    if cfg.vault_id is None:
        return None

    if node_pool is None:
        raise ValueError("An OKE node pool is required to grant Vault access")

    node_pool_group = oci.identity.DynamicGroup(
        "oke-node-pool-dynamic-group",
        compartment_id=cfg.tenancy_id,
        name=OKE_NODE_POOL_DYNAMIC_GROUP_NAME,
        description="Only the compute instances in the OKE node pool",
        matching_rule=node_pool.nodes.apply(node_pool_matching_rule),
    )

    return oci.identity.Policy(
        "oke-vault-read-policy",
        # IAM resources are tenancy-scoped, so this belongs in the tenancy root
        # rather than the configured compartment.
        compartment_id=cfg.tenancy_id,
        name="oke-vault-read-policy",
        description="Read-only OCI Vault access for OKE node instance principals",
        statements=[
            vault_read_statement(cfg.compartment_id, cfg.vault_id, cfg.tenancy_id)
        ],
        opts=pulumi.ResourceOptions(depends_on=[node_pool_group]),
    )
