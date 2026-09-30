"""Write the OKE kubeconfig to ``out/oke_kubeconfig``.

OKE returns a kubeconfig that already points at the public API endpoint, so
there is no server rewrite to do here, unlike the self-managed RKE2 setup.
"""

import os

import pulumi_oci as oci

KUBECONFIG_PATH = os.path.join("out", "oke_kubeconfig")

# The kubeconfig contains a cluster-admin credential, so it is written with
# owner-only permissions rather than the default 0644.
KUBECONFIG_MODE = 0o600


def write_kubeconfig(content, path=KUBECONFIG_PATH):
    """Write kubeconfig content to disk, creating ``out/`` if needed.

    Args:
        content: the kubeconfig YAML from OCI.
        path: where to write it.

    Returns:
        The path written.
    """
    if content is None:
        return None

    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)

    # Write with restrictive permissions from the start rather than chmod'ing
    # after, so the credential is never briefly world readable.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, KUBECONFIG_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)

    return path


def get_kubeconfig(cluster_id):
    """Fetch the admin kubeconfig for a cluster."""
    return oci.containerengine.get_cluster_kube_config_output(cluster_id=cluster_id)
