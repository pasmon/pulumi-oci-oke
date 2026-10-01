"""Load and validate the Pulumi configuration for the OKE cluster.

Every validation failure here happens at ``pulumi preview`` time. Nothing in
this module provisions a resource, which keeps the rules cheap to unit test.
"""

import ipaddress
import re

import pulumi

# Always Free allowances that this program deliberately does not exceed.
#
# Oracle halved the Always Free Ampere A1 allowance on 2026-06-15, enforced
# 2026-08-18: 1500 OCPU hours and 9000 GB hours per month, which is an
# always-on 2 OCPU / 12 GB across the whole tenancy.
ALWAYS_FREE_OCPUS = 2
ALWAYS_FREE_MEMORY_GB = 12

# Block volume Always Free allowance, boot and data volumes combined.
ALWAYS_FREE_BLOCK_VOLUME_GB = 200

# Provisioned size of the shared audio cache PVC that argo-apps creates. The
# claim requests 20Gi, but OCI Block Volume has a 50 GB minimum and rounds the
# request up rather than rejecting it, so 50 is what the tenancy is billed for.
# Budgeting the requested size instead understated usage by 30 GB.
AUDIO_CACHE_GB = 50

# Provisioned size of the radio-db CloudNativePG data volume, for the same
# reason: 50 GB is the OCI Block Volume floor, so no smaller claim is possible.
DATABASE_VOLUME_GB = 50

# Every block volume this tenancy keeps permanently provisioned. Kept together
# so the Always Free check and the README cost table cannot drift apart.
DATA_VOLUME_GB = AUDIO_CACHE_GB + DATABASE_VOLUME_GB

# Each node's tunnel address reuses the last octet of its private IP, so the
# tunnel subnet has to be wide enough for those octets to stay inside it.
MAX_WIREGUARD_PREFIXLEN = 28

# Minimum boot volume OCI accepts for a compute instance.
MIN_BOOT_VOLUME_GB = 50

DEFAULT_KUBERNETES_VERSION = None  # None means "ask OCI for the newest"
DEFAULT_NODE_SHAPE = "VM.Standard.A1.Flex"

# An OCID is "ocid1.<resource type>.<realm>[.<region>].<unique part>". The
# region segment is optional and the unique part may itself be empty, as in a
# tenancy OCID. This deliberately checks shape rather than encoding: OCI signs
# and validates the base64 payload itself.
OCID_PATTERN = re.compile(r"^ocid1\.[a-z0-9]+\.[a-z0-9-]+(\.[a-z0-9-]+)?\..*$")


def _int_or_default(value, default):
    """Parse an integer config value, defaulting only when unset.

    ``value or default`` would silently turn an explicit ``0`` into the
    default, which hides a typo instead of failing validation.
    """
    if value is None or value == "":
        return default
    return int(value)


class Config:
    """Validated view of the stack configuration."""

    def __init__(self, config=None):
        self.config = config or pulumi.Config()

        self.compartment_id = self.config.require("compartment-id")
        self.ssh_public_key_path = self.config.require("ssh-public-key-path")

        # Network
        self.vcn_cidr = self.config.get("vcn-cidr") or "10.10.0.0/16"
        self.endpoint_subnet_cidr = (
            self.config.get("endpoint-subnet-cidr") or "10.10.0.0/24"
        )
        self.nodes_subnet_cidr = self.config.get("nodes-subnet-cidr") or "10.10.1.0/24"
        self.vcn_dns_label = self.config.get("vcn-dns-label") or "radiooke"

        # Kubernetes
        self.pods_cidr = self.config.get("pods-cidr") or "10.244.0.0/16"
        self.services_cidr = self.config.get("services-cidr") or "10.96.0.0/16"
        self.kubernetes_version = (
            self.config.get("kubernetes-version") or DEFAULT_KUBERNETES_VERSION
        )
        # "x or default" would turn an explicit 0 into the default, so these
        # default only on None.
        self.node_count = _int_or_default(self.config.get("node-count"), 2)
        self.node_ocpus = _int_or_default(self.config.get("node-ocpus"), 2)
        self.node_memory_gbs = _int_or_default(self.config.get("node-memory-gbs"), 12)
        self.boot_volume_size_gbs = _int_or_default(
            self.config.get("boot-volume-size-gbs"), MIN_BOOT_VOLUME_GB
        )
        self.ssh_from_anywhere = (
            str(self.config.get("ssh-from-anywhere") or "false").lower() == "true"
        )
        self.idle_guard = (
            str(self.config.get("idle-guard") or "false").lower() == "true"
        )

        # Argo CD / GitOps
        self.argocd_repo_url = self.config.require("argocd-repo-url")
        self.argocd_repo_target_revision = (
            self.config.get("argocd-repo-target-revision") or "main"
        )
        self.argocd_repo_path = (
            self.config.get("argocd-repo-path") or "gitops/bootstrap"
        )
        self.argocd_repo_username = self.config.get("argocd-repo-username")
        self.argocd_repo_password = self.config.get_secret("argocd-repo-password")
        self.argocd_repo_ssh_private_key = self.config.get_secret(
            "argocd-repo-ssh-private-key"
        )
        self.argocd_github_app_id = self.config.get("argocd-github-app-id")
        self.argocd_github_app_installation_id = self.config.get(
            "argocd-github-app-installation-id"
        )
        self.argocd_github_app_private_key = self.config.get_secret(
            "argocd-github-app-private-key"
        )
        self.argocd_managed_by_pulumi = (
            str(self.config.get("argocd-managed-by-pulumi") or "true").lower() == "true"
        )

        # TLS. The Cloudflare token itself lives in OCI Vault and is synced by
        # External Secrets Operator; these two values are not credentials.
        self.tls_domain = self.config.get("tls-domain")
        self.cloudflare_email = self.config.get("cloudflare-email")

        # Workload identity (identifiers only, never credential material)
        self.tenancy_id = self.config.get("tenancy-id")
        self.vault_id = self.config.get("vault-id")
        self.eso_service_account_name = (
            self.config.get("eso-service-account-name") or "external-secrets"
        )
        self.eso_service_account_namespace = (
            self.config.get("eso-service-account-namespace") or "external-secrets"
        )

        # Wireguard
        self.wireguard_peer_endpoint = self.config.get("wireguard-peer-endpoint")
        self.wireguard_peer_public_key = self.config.get("wireguard-peer-public-key")
        self.wireguard_private_key = self.config.get_secret("wireguard-private-key")
        self.wireguard_preshared_key = self.config.get_secret("wireguard-preshared-key")
        self.wireguard_subnet_cidr = (
            self.config.get("wireguard-subnet-cidr") or "10.99.0.0/24"
        )
        self.wireguard_listen_port = _int_or_default(
            self.config.get("wireguard-listen-port"), 51820
        )
        self.wireguard_allowed_cidrs = (
            self.config.get_object("wireguard-allowed-cidrs") or []
        )

        self.validate()

    # ------------------------------------------------------------------ #
    # Derived values
    # ------------------------------------------------------------------ #
    @property
    def node_total_ocpus(self):
        """Total OCPUs across the node pool."""
        return self.node_ocpus * self.node_count

    @property
    def node_total_memory_gbs(self):
        """Total memory across the node pool, in GB."""
        return self.node_memory_gbs * self.node_count

    @property
    def total_block_volume_gb(self):
        """Boot volumes plus every permanently provisioned data volume."""
        return (self.boot_volume_size_gbs * self.node_count) + DATA_VOLUME_GB

    @property
    def wireguard_enabled(self):
        """Whether a Wireguard tunnel to the home router is configured."""
        return self.wireguard_peer_endpoint is not None

    @property
    def wireguard_router_address(self):
        """The router's tunnel address, the first usable address in the subnet."""
        network = ipaddress.ip_network(self.wireguard_subnet_cidr, strict=False)
        return f"{network.network_address + 1}/{network.prefixlen}"

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    def validate(self):
        """Reject invalid configuration before any resource is created."""
        self._validate_cidrs()
        self._validate_nodes()
        self._validate_wireguard()
        self._validate_tls()
        self._validate_ocids()

    def _validate_cidrs(self):
        """Validate every CIDR and reject any overlap between them.

        The VCN is a container for the two subnets, so it is excluded from the
        pairwise overlap check; the subnets are checked against each other.
        """
        named = {
            "endpoint-subnet-cidr": self.endpoint_subnet_cidr,
            "nodes-subnet-cidr": self.nodes_subnet_cidr,
            "pods-cidr": self.pods_cidr,
            "services-cidr": self.services_cidr,
        }
        try:
            vcn = ipaddress.ip_network(self.vcn_cidr, strict=True)
        except ValueError as error:
            raise ValueError(f"Invalid vcn-cidr {self.vcn_cidr!r}: {error}") from error

        networks = {}
        for name, cidr in named.items():
            try:
                networks[name] = ipaddress.ip_network(cidr, strict=True)
            except ValueError as error:
                raise ValueError(f"Invalid {name} {cidr!r}: {error}") from error

        for name in ("endpoint-subnet-cidr", "nodes-subnet-cidr"):
            if not networks[name].subnet_of(vcn):
                raise ValueError(
                    f"{name} {networks[name]} is not inside vcn-cidr {vcn}"
                )

        # Pod and service ranges must sit outside the VCN entirely, otherwise
        # node-to-pod traffic and the tunnel would collide with VNIC addresses.
        for name in ("pods-cidr", "services-cidr"):
            if networks[name].overlaps(vcn):
                raise ValueError(
                    f"{name} {networks[name]} overlaps vcn-cidr {vcn}; "
                    "pods and services must be routable on the pod network, not inside the VCN"
                )

        for index, (left_name, left) in enumerate(networks.items()):
            for right_name, right in list(networks.items())[index + 1 :]:
                if left.overlaps(right):
                    raise ValueError(
                        f"{left_name} {left} overlaps {right_name} {right}"
                    )

        if networks["endpoint-subnet-cidr"] == networks["nodes-subnet-cidr"]:
            raise ValueError("endpoint-subnet-cidr and nodes-subnet-cidr must differ")

    def _validate_nodes(self):
        """Warn about Always Free overage and reject impossible boot volumes."""
        if self.node_count < 1:
            raise ValueError("node-count must be at least 1")
        if self.node_ocpus < 1:
            raise ValueError("node-ocpus must be at least 1")
        if self.boot_volume_size_gbs < MIN_BOOT_VOLUME_GB:
            raise ValueError(
                f"boot-volume-size-gbs must be at least {MIN_BOOT_VOLUME_GB}, "
                f"got {self.boot_volume_size_gbs}"
            )

        # The allowance is an inclusive ceiling and the defaults land exactly on
        # it, so this is `>=` rather than `>`. Reaching the ceiling is not
        # rejected: a tenancy sitting at 200 GB is still inside Always Free,
        # and refusing to preview it would make the configuration unusable.
        # What matters is that there is no headroom left, which is a cost
        # decision rather than an impossible configuration. It therefore warns,
        # like the node pool overage below, rather than raising.
        if self.total_block_volume_gb >= ALWAYS_FREE_BLOCK_VOLUME_GB:
            print(
                "\n"
                "  ================================================================\n"
                "   COST WARNING: block volumes leave no Always Free headroom\n"
                "  ================================================================\n"
                f"   boot volumes     : {self.boot_volume_size_gbs * self.node_count} GB "
                f"({self.node_count} x {self.boot_volume_size_gbs} GB)\n"
                f"   audio cache      : {AUDIO_CACHE_GB} GB\n"
                f"   radio database   : {DATABASE_VOLUME_GB} GB\n"
                f"   total            : {self.total_block_volume_gb} GB\n"
                f"   Always Free allows: {ALWAYS_FREE_BLOCK_VOLUME_GB} GB per tenancy\n"
                "\n"
                "   This is still inside Always Free, but nothing else fits. Both data\n"
                "   volumes are already at the 50 GB OCI Block Volume minimum, so they\n"
                "   cannot be made smaller, and expanding either one bills immediately.\n"
                "   This also rules out volumeSnapshot backups for the database, which\n"
                "   draw on the same allowance. To create headroom, drop a node:\n"
                "\n"
                "       pulumi config set node-count 1\n"
                "\n"
            )

        if (
            self.node_total_ocpus > ALWAYS_FREE_OCPUS
            or self.node_total_memory_gbs > ALWAYS_FREE_MEMORY_GB
        ):
            print(
                "\n"
                "  ================================================================\n"
                "   COST WARNING: the node pool exceeds the Always Free allowance\n"
                "  ================================================================\n"
                f"   node-count        : {self.node_count}\n"
                f"   per node          : {self.node_ocpus} OCPU / {self.node_memory_gbs} GB\n"
                f"   total             : {self.node_total_ocpus} OCPU / {self.node_total_memory_gbs} GB\n"
                f"   Always Free allows: {ALWAYS_FREE_OCPUS} OCPU / {ALWAYS_FREE_MEMORY_GB} GB per tenancy\n"
                "\n"
                "   Oracle reduced this allowance on 2026-06-15 and began enforcing it\n"
                "   on 2026-08-18. A tenancy that exceeds it will be billed for the\n"
                "   overage on Pay-As-You-Go, or have its instances terminated on Free\n"
                "   Tier. To stay inside Always Free:\n"
                "\n"
                "       pulumi config set node-ocpus 1\n"
                "       pulumi config set node-memory-gbs 6\n"
                "\n"
            )

    def _validate_wireguard(self):
        """Require the Wireguard peer fields to be set together, or not at all."""
        if not self.wireguard_enabled:
            return

        if self.wireguard_peer_public_key is None or self.wireguard_private_key is None:
            raise ValueError(
                "Set wireguard-peer-endpoint, wireguard-peer-public-key, and "
                "wireguard-private-key together, or omit them all."
            )
        if self.wireguard_preshared_key is None:
            raise ValueError(
                "Set wireguard-preshared-key when wireguard-peer-endpoint is configured."
            )

        try:
            tunnel = ipaddress.ip_network(self.wireguard_subnet_cidr, strict=False)
        except ValueError as error:
            raise ValueError(f"Invalid wireguard-subnet-cidr: {error}") from error

        # The tunnel must not collide with any cluster network.
        others = {
            "vcn-cidr": self.vcn_cidr,
            "pods-cidr": self.pods_cidr,
            "services-cidr": self.services_cidr,
        }
        for name, cidr in others.items():
            other = ipaddress.ip_network(cidr, strict=False)
            if tunnel.overlaps(other):
                raise ValueError(
                    f"wireguard-subnet-cidr {tunnel} overlaps {name} {other}"
                )

        # The derived address reuses the node's private IP last octet, so the
        # tunnel subnet must be large enough that a node with a high host
        # address still lands inside it. A /28 or larger is the practical floor.
        if tunnel.prefixlen > MAX_WIREGUARD_PREFIXLEN:
            raise ValueError(
                f"wireguard-subnet-cidr {tunnel} is too small; use a /{MAX_WIREGUARD_PREFIXLEN} "
                "or larger so every node's derived address lands inside the tunnel subnet"
            )

        for cidr in self.wireguard_allowed_cidrs:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as error:
                raise ValueError(
                    f"Invalid wireguard-allowed-cidrs entry {cidr!r}: {error}"
                ) from error

    def _validate_tls(self):
        """Require the TLS domain and ACME email together."""
        if bool(self.tls_domain) != bool(self.cloudflare_email):
            raise ValueError(
                "Set tls-domain and cloudflare-email together, or omit them both."
            )

    def _validate_ocids(self):
        """Reject values that are not shaped like OCIDs.

        A wrong tenancy here produces an opaque OCI policy-assignment failure
        much later, so it is worth catching at preview time.
        """
        if self.tenancy_id is not None and not OCID_PATTERN.match(self.tenancy_id):
            raise ValueError(
                f"tenancy-id {self.tenancy_id!r} does not look like an OCID"
            )
        if self.vault_id is not None and not OCID_PATTERN.match(self.vault_id):
            raise ValueError(f"vault-id {self.vault_id!r} does not look like an OCID")
