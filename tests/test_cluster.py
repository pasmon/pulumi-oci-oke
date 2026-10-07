"""Unit tests for cluster version and image selection.

The pure functions are tested directly; the Pulumi wiring is exercised by
``test_program_wiring.py`` under the engine mocks.
"""

from types import SimpleNamespace

import pytest

from oke import cluster
from oke.config import DEFAULT_NODE_SHAPE

# A real source name from GetNodePoolOptions. The version sits after the OKE
# marker, not straight after the architecture as it did in older images.
ARM_SOURCE = "Oracle-Linux-8.10-aarch64-2026.08.14-0-OKE-1.36.4-1820"
X86_SOURCE = "Oracle-Linux-8.10-2026.08.14-0-OKE-1.36.4-1820"


def fake_source(image_id, source_name, source_type="IMAGE", as_dict=False):
    """Build a node pool source in either of the shapes seen in practice.

    The provider documents these as objects but returns dicts, and reading the
    wrong shape yields nothing rather than failing, so both are exercised.
    """
    fields = {
        "image_id": image_id,
        "source_name": source_name,
        "source_type": source_type,
    }
    return fields if as_dict else SimpleNamespace(**fields)


class TestVersionSortKey:
    """Version strings sort numerically, not lexically."""

    def test_leading_v_is_ignored(self):
        assert cluster.version_sort_key("v1.31.1") == (1, 31, 1)

    def test_two_digit_minors_sort_correctly(self):
        # Lexically "1.9" would sort after "1.31", which is wrong.
        assert cluster.version_sort_key("v1.9.0") < cluster.version_sort_key("v1.31.0")

    def test_pre_release_suffix_is_ignored(self):
        assert cluster.version_sort_key("v1.32.0-rc.1") == (1, 32, 0)

    def test_missing_components_are_padded(self):
        assert cluster.version_sort_key("1.31") == (1, 31, 0)


class TestSelectKubernetesVersion:
    """The newest advertised version wins unless overridden."""

    def test_newest_is_selected(self):
        versions = ["v1.29.1", "v1.31.1", "v1.30.0"]
        assert cluster.select_kubernetes_version(versions) == "v1.31.1"

    def test_order_of_input_does_not_matter(self):
        assert cluster.select_kubernetes_version(["v1.31.1", "v1.29.1"]) == "v1.31.1"

    def test_override_wins(self):
        assert cluster.select_kubernetes_version(["v1.31.1"], "v1.30.5") == "v1.30.5"

    def test_empty_list_is_rejected(self):
        with pytest.raises(ValueError, match="advertised no Kubernetes versions"):
            cluster.select_kubernetes_version([])

    def test_none_is_rejected(self):
        with pytest.raises(ValueError, match="advertised no Kubernetes versions"):
            cluster.select_kubernetes_version(None)


class TestSelectArmImage:
    """Only an ARM image is usable by the Always Free shape."""

    def test_aarch_image_is_selected(self):
        sources = [
            fake_source("ocid1.image.x86", X86_SOURCE),
            fake_source("ocid1.image.arm", ARM_SOURCE),
        ]
        assert cluster.select_arm_image(sources) == "ocid1.image.arm"

    def test_dict_sources_are_read(self):
        # The provider returns dicts. Reading them as objects finds no ARM image
        # and fails the whole plan.
        sources = [
            fake_source("ocid1.image.x86", X86_SOURCE, as_dict=True),
            fake_source("ocid1.image.arm", ARM_SOURCE, as_dict=True),
        ]
        assert cluster.select_arm_image(sources) == "ocid1.image.arm"

    def test_arm64_spelling_is_also_matched(self):
        sources = [
            fake_source("ocid1.image.arm", "Oracle-Kubernetes-Engine-arm64-1.31.1")
        ]
        assert cluster.select_arm_image(sources) == "ocid1.image.arm"

    def test_no_arm_image_is_an_error(self):
        sources = [fake_source("ocid1.image.x86", X86_SOURCE)]
        with pytest.raises(ValueError, match="no ARM node image"):
            cluster.select_arm_image(sources)

    def test_empty_sources_is_an_error(self):
        with pytest.raises(ValueError, match="no ARM node image"):
            cluster.select_arm_image([])

    def test_source_without_an_image_id_is_skipped(self):
        sources = [
            fake_source(None, ARM_SOURCE),
            fake_source("ocid1.image.arm", ARM_SOURCE),
        ]
        assert cluster.select_arm_image(sources) == "ocid1.image.arm"


class TestImageMatchesVersion:
    """The node image has to correspond to the cluster version."""

    def test_exact_version_match(self):
        assert cluster.image_matches_version(fake_source("id", ARM_SOURCE), "v1.36.4")

    def test_leading_v_is_handled(self):
        assert cluster.image_matches_version(fake_source("id", ARM_SOURCE), "1.36.4")

    def test_dict_source_is_read(self):
        source = fake_source("id", ARM_SOURCE, as_dict=True)
        assert cluster.image_matches_version(source, "v1.36.4")

    def test_major_minor_fallback(self):
        source = fake_source("id", "Oracle-Linux-8.10-aarch64-2026.08.14-0-OKE-1.36.0")
        assert cluster.image_matches_version(source, "v1.36.4")

    def test_different_minor_does_not_match(self):
        source = fake_source("id", "Oracle-Linux-8.10-aarch64-2026.01.01-0-OKE-1.29.1")
        assert not cluster.image_matches_version(source, "v1.36.4")

    def test_name_without_a_version_does_not_match(self):
        # Real, but unversioned: OKE also lists the bare platform images.
        source = fake_source("id", "Oracle-Linux-8.10-aarch64-2026.09.18-0")
        assert not cluster.image_matches_version(source, "v1.36.4")


class TestNodeShape:
    """The node shape is the Always Free Ampere A1 flexible shape."""

    def test_shape_is_a1_flex(self):
        assert DEFAULT_NODE_SHAPE == "VM.Standard.A1.Flex"

    def test_node_pool_is_queried_for_arm(self):
        # GetNodePoolOptions rejects anything outside these enums: "ARM_64" and
        # "LINUX" both fail at preview, and the arch value is not the one OCI
        # uses in shape names.
        assert cluster.NODE_OS_ARCH == "AARCH64"
        assert cluster.NODE_OS_TYPE == "OL8"

    def test_node_pool_option_is_the_full_set(self):
        assert cluster.NODE_POOL_OPTION_ID == "all"
