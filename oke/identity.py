"""The IAM policy that lets cluster nodes read OCI Vault.

External Secrets Operator authenticates to OCI Vault as an *instance principal*:
the identity of the compute instance the pod happens to run on. Every tenancy
already has a dynamic group named ``oke`` whose rule matches every OKE node in
it, so this program creates the policy and nothing else.

Workload Identity would be finer grained, but OKE only offers it on *enhanced*
clusters, which are billed at $0.10/cluster-hour. This program creates a basic
cluster on purpose to stay inside Always Free, so instance principals are the
only credential-free option available. The cost of that choice is scope: any pod
on a node can read the vault, not just ESO. The policy is therefore restricted
to one vault and to ``read``, which is the smallest grant that works.
"""

import pulumi_oci as oci

# OCI creates this dynamic group in every tenancy. Its rule matches the tenancy's
# OKE node instances, which is exactly the set of principals ESO runs as. It is a
# constant rather than a Pulumi resource because OCI owns it: a Pulumi-managed
# group of the same name would collide with it.
OKE_DYNAMIC_GROUP_NAME = "oke"

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


def vault_read_statement(compartment_id, vault_id, tenancy_id=None):
    """Build the IAM statement granting node principals read on one vault.

    The principal is a compute instance rather than a pod, so the statement
    names ``request.principal.type = 'instance'`` rather than any Kubernetes
    coordinates. Scoping by ``target.vault.id`` is what keeps the grant from
    covering every vault in the compartment.

    Args:
        compartment_id: the compartment holding the vault.
        vault_id: the vault OCID.
        tenancy_id: the tenancy root OCID, used to detect the root compartment.

    Returns:
        The statement, ready for ``oci.identity.Policy``.
    """
    return (
        f"Allow group {OKE_DYNAMIC_GROUP_NAME} to read {VAULT_READ_PERMISSION} "
        f"in {statement_scope(compartment_id, tenancy_id)} where all {{"
        f"request.principal.type = 'instance', "
        f"target.vault.id = '{vault_id}'"
        f"}}"
    )


def create_vault_read_policy(cfg):
    """Create the read-only vault policy for the cluster's node principals.

    Args:
        cfg: the validated :class:`oke.config.Config`.

    Returns:
        The ``oci.identity.Policy`` resource, or ``None`` when ``vault-id`` is
        not configured.
    """
    if cfg.vault_id is None:
        return None

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
    )
