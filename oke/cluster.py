"""OKE cluster and ARM node pool.

Nothing here is hardcoded: the availability domains, the Kubernetes version and
the node image are all discovered from OCI at plan time. The OKE cluster is a
*basic* cluster because enhanced clusters are billed hourly.
"""

import pulumi
import pulumi_oci as oci

from oke.config import DEFAULT_NODE_SHAPE

NODE_POOL_OPTION_ID = "all"

# The node pool is ARM, because the Always Free allowance only covers Ampere A1.
NODE_OS_ARCH = "ARM_64"
NODE_OS_TYPE = "LINUX"

# Belt and braces: OCI already filters by the arguments above, but an ARM image
# is what the plan depends on, so the chosen source name is asserted too.
ARM_IMAGE_MARKERS = ("aarch", "arm64")


def select_kubernetes_version(versions, override=None):
    """Pick the Kubernetes version to deploy.

    Args:
        versions: the version strings OCI advertises, unsorted.
        override: an explicit version from the stack configuration.

    Returns:
        The version string to pass to the cluster and node pool.
    """
    if override:
        return override

    advertised = [version for version in versions or [] if version]
    if not advertised:
        raise ValueError(
            "OKE advertised no Kubernetes versions; check the region and compartment"
        )

    return max(advertised, key=version_sort_key)


def version_sort_key(version):
    """Sort key for a Kubernetes version string.

    Handles the ``v1.31.1`` shape, tolerating a leading ``v`` and any
    pre-release suffix such as ``v1.32.0-rc.1``.
    """
    core = version.lstrip("vV").split("-", 1)[0]
    parts = []
    for component in core.split("."):
        try:
            parts.append(int(component))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def select_arm_image(sources):
    """Pick the ARM node image from the node pool option sources.

    Args:
        sources: the source objects OCI advertises for this architecture.

    Returns:
        The image OCID.

    Raises:
        ValueError: if no ARM image is offered, which would mean the region
            cannot host the Always Free node shape.
    """
    for source in sources or []:
        name = (getattr(source, "source_name", None) or "").lower()
        image_id = getattr(source, "image_id", None)
        if not image_id:
            continue
        if any(marker in name for marker in ARM_IMAGE_MARKERS):
            return image_id

    raise ValueError(
        "OKE offered no ARM node image for this region; the Always Free "
        "VM.Standard.A1.Flex shape cannot be provisioned without one"
    )


def create_cluster(
    cfg, vcn_id, endpoint_subnet_id, nodes_subnet_id, node_metadata=None
):
    """Create the OKE cluster and its node pool.

    Args:
        cfg: the validated :class:`oke.config.Config`.
        vcn_id: the VCN that hosts both subnets.
        endpoint_subnet_id: the public subnet that carries the API endpoint.
        nodes_subnet_id: the public subnet that carries the worker nodes.
        node_metadata: optional node user data, base64 encoded already.

    Returns:
        A dict with the cluster, node pool and the discovered values.
    """
    availability_domains = oci.identity.get_availability_domains_output(
        compartment_id=cfg.compartment_id
    ).availability_domains.apply(lambda domains: [domain.name for domain in domains])

    node_pool_option = oci.containerengine.get_node_pool_option_output(
        compartment_id=cfg.compartment_id,
        node_pool_option_id=NODE_POOL_OPTION_ID,
        node_pool_os_arch=NODE_OS_ARCH,
        node_pool_os_type=NODE_OS_TYPE,
        should_list_all_patch_versions=True,
    )

    kubernetes_version = node_pool_option.kubernetes_versions.apply(
        lambda versions: select_kubernetes_version(versions, cfg.kubernetes_version)
    )

    node_image_id = pulumi.Output.all(
        kubernetes_version, node_pool_option.sources
    ).apply(
        lambda values: select_arm_image(
            [source for source in values[1] if image_matches_version(source, values[0])]
        )
    )

    cluster = oci.containerengine.Cluster(
        "oke-cluster",
        compartment_id=cfg.compartment_id,
        vcn_id=vcn_id,
        kubernetes_version=kubernetes_version,
        name="oke-cluster",
        endpoint_config=oci.containerengine.ClusterEndpointConfigArgs(
            is_public_ip_enabled=True,
            subnet_id=endpoint_subnet_id,
        ),
        options=oci.containerengine.ClusterOptionsArgs(
            add_ons=oci.containerengine.ClusterOptionsAddOnsArgs(
                is_kubernetes_dashboard_enabled=False,
                is_tiller_enabled=False,
            ),
            kubernetes_network_config=oci.containerengine.ClusterOptionsKubernetesNetworkConfigArgs(
                pods_cidr=cfg.pods_cidr,
                services_cidr=cfg.services_cidr,
            ),
            service_lb_subnet_ids=[nodes_subnet_id],
        ),
    )

    # One placement config per availability domain. A single node shape in a
    # single AD is a common cause of "out of host capacity" on the free tier.
    placement_configs = availability_domains.apply(
        lambda names: [
            oci.containerengine.NodePoolNodeConfigDetailsPlacementConfigArgs(
                availability_domain=name,
                subnet_id=nodes_subnet_id,
            )
            for name in names
        ]
    )

    source_details_args = {
        "image_id": node_image_id,
        "source_type": "image",
        "boot_volume_size_in_gbs": cfg.boot_volume_size_gbs,
    }
    # node_metadata is a plain string map, not an args class.
    metadata_args = {"user_data": node_metadata} if node_metadata else None

    node_pool = oci.containerengine.NodePool(
        "oke-node-pool",
        compartment_id=cfg.compartment_id,
        cluster_id=cluster.id,
        kubernetes_version=kubernetes_version,
        name="oke-node-pool",
        node_shape=DEFAULT_NODE_SHAPE,
        node_shape_config=oci.containerengine.NodePoolNodeShapeConfigArgs(
            ocpus=cfg.node_ocpus,
            memory_in_gbs=cfg.node_memory_gbs,
        ),
        node_config_details=oci.containerengine.NodePoolNodeConfigDetailsArgs(
            placement_configs=placement_configs,
            size=cfg.node_count,
        ),
        node_source_details=oci.containerengine.NodePoolNodeSourceDetailsArgs(
            **source_details_args
        ),
        node_metadata=metadata_args,
        ssh_public_key=_read_ssh_public_key(cfg),
        # Force replacement when the image or version changes, otherwise the
        # node pool keeps the old bootstrap data.
        opts=pulumi.ResourceOptions(depends_on=[cluster], delete_before_replace=True),
    )

    return {
        "cluster": cluster,
        "node_pool": node_pool,
        "availability_domains": availability_domains,
        "kubernetes_version": kubernetes_version,
        "node_image_id": node_image_id,
    }


def image_matches_version(source, version):
    """Whether a node pool source is an OKE-optimized image for this version."""
    name = (getattr(source, "source_name", None) or "").lower()
    bare_version = version.lstrip("vV")
    # OKE-optimized ARM images look like "Oracle-Kubernetes-Engine-aarch64-1.31.1".
    if f"-{bare_version}" in name:
        return True
    # Fall back to a major.minor match so a patch-level image name difference
    # does not leave the node pool without an image.
    short = ".".join(bare_version.split(".")[:2])
    return f"-{short}." in name or f"-{short}" in name


def _read_ssh_public_key(cfg):
    """Read the SSH public key that OKE installs on every node."""
    with open(cfg.ssh_public_key_path, "r", encoding="utf-8") as key_file:
        return key_file.read()
