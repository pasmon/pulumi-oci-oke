"""Unit tests for cluster version and image selection.

The pure functions are tested directly; the Pulumi wiring is exercised by
``test_program_wiring.py`` under the engine mocks.
"""

import pytest

from oke import cluster


class FakeSource:
    """Stand-in for an OCI node pool source object."""

    def __init__(self, image_id, source_name, source_type="IMAGE"):
        self.image_id = image_id
        self.source_name = source_name
        self.source_type = source_type


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
            FakeSource("ocid1.image.x86", "Oracle-Kubernetes-Engine-1.31.1"),
            FakeSource(
                "ocid1.image.arm", "Oracle-Kubernetes-Engine-aarch64-1.31.1-20260101"
            ),
        ]
        assert cluster.select_arm_image(sources) == "ocid1.image.arm"

    def test_arm64_spelling_is_also_matched(self):
        sources = [
            FakeSource("ocid1.image.arm", "Oracle-Kubernetes-Engine-arm64-1.31.1")
        ]
        assert cluster.select_arm_image(sources) == "ocid1.image.arm"

    def test_no_arm_image_is_an_error(self):
        sources = [FakeSource("ocid1.image.x86", "Oracle-Kubernetes-Engine-1.31.1")]
        with pytest.raises(ValueError, match="no ARM node image"):
            cluster.select_arm_image(sources)

    def test_empty_sources_is_an_error(self):
        with pytest.raises(ValueError, match="no ARM node image"):
            cluster.select_arm_image([])

    def test_source_without_an_image_id_is_skipped(self):
        sources = [
            FakeSource(None, "Oracle-Kubernetes-Engine-aarch64-1.31.1"),
            FakeSource("ocid1.image.arm", "Oracle-Kubernetes-Engine-aarch64-1.31.1-ok"),
        ]
        assert cluster.select_arm_image(sources) == "ocid1.image.arm"


class TestImageMatchesVersion:
    """The node image has to correspond to the cluster version."""

    def test_exact_version_match(self):
        source = FakeSource("id", "Oracle-Kubernetes-Engine-aarch64-1.31.1-20260101")
        assert cluster.image_matches_version(source, "v1.31.1")

    def test_leading_v_is_handled(self):
        source = FakeSource("id", "Oracle-Kubernetes-Engine-aarch64-1.31.1-20260101")
        assert cluster.image_matches_version(source, "1.31.1")

    def test_major_minor_fallback(self):
        source = FakeSource("id", "Oracle-Kubernetes-Engine-aarch64-1.31.0")
        assert cluster.image_matches_version(source, "v1.31.1")

    def test_different_minor_does_not_match(self):
        source = FakeSource("id", "Oracle-Kubernetes-Engine-aarch64-1.29.1-20260101")
        assert not cluster.image_matches_version(source, "v1.31.1")

    def test_name_without_a_version_does_not_match(self):
        source = FakeSource("id", "Oracle-Linux-8.10-aarch64")
        assert not cluster.image_matches_version(source, "v1.31.1")


class TestNodeShape:
    """The node shape is the Always Free Ampere A1 flexible shape."""

    def test_shape_is_a1_flex(self):
        from oke.config import DEFAULT_NODE_SHAPE

        assert DEFAULT_NODE_SHAPE == "VM.Standard.A1.Flex"

    def test_node_pool_is_queried_for_arm(self):
        assert cluster.NODE_OS_ARCH == "ARM_64"
        assert cluster.NODE_OS_TYPE == "LINUX"

    def test_node_pool_option_is_the_full_set(self):
        assert cluster.NODE_POOL_OPTION_ID == "all"
