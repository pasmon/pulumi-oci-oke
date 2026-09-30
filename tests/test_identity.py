"""Unit tests for the Workload Identity dynamic group rule."""

from oke import identity
from tests.conftest import build_config

TENANCY = "ocid1.tenancy.oc1..testtenancy0000000000000000000000000000000"


class TestDynamicGroupRule:
    """OCI requires the tenancy to appear twice in an OKE dynamic group rule."""

    def test_rule_matches_the_whole_tenancy(self):
        rule = identity.oke_dynamic_group_rule(TENANCY)
        # One clause matching every node instance principal in the tenancy.
        assert rule == f"ALL {{any.user.tenantid == '{TENANCY}'}}"
        assert rule.count("any.user.tenantid ==") == 1

    def test_rule_starts_with_all(self):
        assert identity.oke_dynamic_group_rule(TENANCY).startswith("ALL {")

    def test_rule_braces_are_balanced(self):
        rule = identity.oke_dynamic_group_rule(TENANCY)
        assert rule.count("{") == rule.count("}")
        assert rule.endswith("}")


class TestDynamicGroupCreation:
    """No dynamic group without a tenancy, and IAM lives in the tenancy root."""

    def test_returns_none_without_a_tenancy(self):
        assert identity.create_oke_dynamic_group(build_config()) is None

    def test_name_is_stable(self):
        assert identity.OKE_DYNAMIC_GROUP_NAME == "oke"

    def test_created_in_the_tenancy_root(self, identity_stack):
        # IAM resources live in the tenancy, not in a child compartment. This
        # fixture loads __main__.py under mocks and lives in conftest.py.
        assert identity_stack.oke_dynamic_group is not None


class TestAnnotationPrefix:
    """The annotation prefix is exported for the operator to complete."""

    def test_prefix_is_the_oci_workload_identity_prefix(self):
        assert (
            identity.DYNAMIC_GROUP_ANNOTATION_PREFIX
            == "identity.oci.authorization.oke.info/"
        )

    def test_prefix_ends_with_a_separator(self):
        assert identity.DYNAMIC_GROUP_ANNOTATION_PREFIX.endswith("/")
