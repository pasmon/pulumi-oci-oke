"""Unit tests for the Workload Identity dynamic group rule."""

import pytest

from oke import identity
from tests.conftest import build_config

TENANCY = "ocid1.tenancy.oc1..testtenancy0000000000000000000000000000000"
CLUSTER = "ocid1.cluster.oc1.eu-stockholm-1.testcluster000000000000000000"
GROUP = "ocid1.dynamicgroup.oc1..testgroup000000000000000000000000000000"

# The annotation names the group, not its OCID, because of the 63-byte limit.
GROUP_NAME = "oke-radio"


class TestDynamicGroupRule:
    """OCI requires the tenancy to appear twice in an OKE dynamic group rule."""

    def test_rule_matches_the_whole_tenancy(self):
        rule = identity.oke_dynamic_group_rule(TENANCY)
        # One clause matching every node instance principal in the tenancy.
        assert rule == f"ALL {{any.user.tenantid = '{TENANCY}'}}"
        assert rule.count("any.user.tenantid =") == 1

    def test_comparison_is_a_single_equals(self):
        # IDCS rejects "==" as an unparseable rule, so the create fails and the
        # policy is never what looks wrong.
        assert "==" not in identity.oke_dynamic_group_rule(TENANCY)

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

    def test_annotation_prefix_is_the_oci_workload_identity_prefix(self):
        assert (
            identity.DYNAMIC_GROUP_ANNOTATION_PREFIX
            == "identity.oci.authorization.oke.info/"
        )

    def test_created_in_the_tenancy_root(self, identity_stack):
        # IAM resources live in the tenancy, not in a child compartment. This
        # fixture loads __main__.py under mocks and lives in conftest.py.
        assert identity_stack.oke_dynamic_group is not None


class TestSpiffeId:
    """The SPIFFE ID has to match the dynamic group rule exactly.

    A mismatch here does not fail deployment, it fails authentication at the
    Vault, which is much harder to read. Hence the exact shape.
    """

    def test_id_names_the_cluster_namespace_and_account(self):
        assert (
            identity.spiffe_id(CLUSTER, "external-secrets", "external-secrets")
            == f"spiffe://{CLUSTER}/ns/external-secrets/sa/external-secrets"
        )

    def test_id_starts_with_the_spiffe_scheme(self):
        assert identity.spiffe_id(CLUSTER, "ns", "sa").startswith("spiffe://")

    def test_id_uses_the_separators_oke_expects(self):
        # /ns/ and /sa/ are part of OKE's subject format, not decoration.
        identity_id = identity.spiffe_id(CLUSTER, "ns", "sa")
        assert "/ns/ns/sa/sa" in identity_id


class TestWorkloadIdentityAnnotations:
    """The annotation key names the group and the value is the SPIFFE ID."""

    def test_key_is_the_prefix_plus_the_group_name(self):
        annotations = identity.workload_identity_annotations(
            GROUP_NAME, CLUSTER, "external-secrets", "external-secrets"
        )
        key = next(iter(annotations))
        assert key == f"identity.oci.authorization.oke.info/{GROUP_NAME}"

    def test_the_group_name_matches_what_is_created(self):
        # The annotation and the resource must not drift: OKE matches the
        # annotation's suffix against the group's name.
        assert identity.OKE_DYNAMIC_GROUP_FULL_NAME == GROUP_NAME

    def test_key_fits_the_annotation_name_limit(self):
        # An OCID is 84 bytes and Kubernetes caps an annotation name part at 63,
        # so an OCID key is rejected by the API server before OKE reads it.
        annotations = identity.workload_identity_annotations(
            GROUP_NAME, CLUSTER, "external-secrets", "external-secrets"
        )
        name_part = next(iter(annotations)).split("/")[-1]
        assert len(name_part.encode("utf-8")) <= identity.ANNOTATION_NAME_MAX_BYTES

    def test_the_key_is_a_plain_string_not_an_output(self):
        # A DynamicGroup resource's `name` is an Output, and stringifying one
        # puts the warning text in the key. The name must be passed as a string.
        annotations = identity.workload_identity_annotations(
            identity.OKE_DYNAMIC_GROUP_FULL_NAME,
            CLUSTER,
            "external-secrets",
            "external-secrets",
        )
        assert isinstance(next(iter(annotations)), str)

    def test_a_group_name_that_is_too_long_is_rejected(self):
        # Caught here rather than by the API server, which reports it as an
        # invalid annotation on an otherwise correct ServiceAccount.
        with pytest.raises(ValueError, match="annotation key limit"):
            identity.workload_identity_annotations(
                "x" * (identity.ANNOTATION_NAME_MAX_BYTES + 1),
                CLUSTER,
                "external-secrets",
                "external-secrets",
            )

    def test_value_is_the_spiffe_id(self):
        annotations = identity.workload_identity_annotations(
            GROUP_NAME, CLUSTER, "external-secrets", "external-secrets"
        )
        assert (
            next(iter(annotations.values()))
            == f"spiffe://{CLUSTER}/ns/external-secrets/sa/external-secrets"
        )

    def test_exactly_one_annotation(self):
        # OKE reads the key's suffix as the group to bind. A second annotation
        # would either be ignored or, worse, bind a second policy.
        annotations = identity.workload_identity_annotations(
            GROUP_NAME, CLUSTER, "external-secrets", "external-secrets"
        )
        assert len(annotations) == 1

    def test_carries_no_credential(self):
        annotations = identity.workload_identity_annotations(
            GROUP_NAME, CLUSTER, "external-secrets", "external-secrets"
        )
        rendered = "".join(f"{key}{value}" for key, value in annotations.items())
        assert "BEGIN " not in rendered
        assert "fingerprint" not in rendered


class TestServiceAccountCreation:
    """The ServiceAccount exists only once Workload Identity is configured."""

    def test_absent_without_a_dynamic_group(self):
        assert (
            identity.create_eso_service_account(
                build_config(), "kubeconfig", CLUSTER, None
            )
            is None
        )

    def test_name_and_namespace_come_from_config(self, identity_stack):
        # The ClusterSecretStore in argo-apps references this ServiceAccount by
        # name, so the two must not drift apart silently.
        cfg = identity_stack.cfg
        assert cfg.eso_service_account_name == "external-secrets"
        assert cfg.eso_service_account_namespace == "external-secrets"

    def test_namespace_is_created_by_pulumi(self, identity_stack):
        # A ServiceAccount cannot be created before its namespace exists, and
        # the GitOps repo's ESO Application is not synced yet at this point.
        assert identity_stack.created_namespaces["external-secrets"] is not None
