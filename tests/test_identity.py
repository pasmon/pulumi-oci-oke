"""Unit tests for the vault read policy."""

import pytest

from oke import identity
from tests.conftest import build_config

COMPARTMENT = "ocid1.compartment.oc1..testcompartment000000000000000000"
VAULT = "ocid1.vault.oc1.eu-stockholm-1.testvault0000000000000000000000"


class TestVaultReadStatement:
    """The grant is read-only, vault-scoped, and aimed at instance principals."""

    def test_names_the_oke_dynamic_group(self):
        # OCI creates this group in every tenancy; Pulumi must not create it.
        assert identity.create_vault_read_policy(build_config()) is None
        statement = identity.vault_read_statement(COMPARTMENT, VAULT)
        assert f"Allow group {identity.OKE_DYNAMIC_GROUP_NAME} to read" in statement

    def test_is_read_only(self):
        # "manage" would hand every pod on a node write access to the vault.
        statement = identity.vault_read_statement(COMPARTMENT, VAULT)
        assert " to manage" not in statement
        assert f"to read {identity.VAULT_READ_PERMISSION}" in statement

    def test_scopes_to_the_one_vault(self):
        # Without target.vault.id the grant covers every vault in the compartment.
        statement = identity.vault_read_statement(COMPARTMENT, VAULT)
        assert f"target.vault.id = '{VAULT}'" in statement

    def test_selects_instance_principals(self):
        # There is no Kubernetes coordinate in an instance principal's request.
        statement = identity.vault_read_statement(COMPARTMENT, VAULT)
        assert "request.principal.type = 'instance'" in statement
        assert "service_account" not in statement
        assert "namespace" not in statement

    def test_is_scoped_to_the_vault_compartment(self):
        statement = identity.vault_read_statement(
            COMPARTMENT, VAULT, "ocid1.tenancy.oc1..root"
        )
        assert f"in compartment {COMPARTMENT}" in statement

    def test_scopes_to_tenancy_when_the_vault_is_in_the_root(self):
        # A tenancy OCID on the left of the statement makes CreatePolicy answer
        # 400 "Compartment {...} does not exist or is not part of the policy
        # compartment subtree". The root is spelled "tenancy".
        root = "ocid1.tenancy.oc1..aaaaaaaaroot0000000000000000000000"
        statement = identity.vault_read_statement(root, VAULT, root)
        assert "in tenancy where all" in statement
        assert root not in statement.split("where all")[0]

    def test_condition_block_is_closed(self):
        # An unbalanced brace makes OCI reject the whole policy at apply.
        statement = identity.vault_read_statement(COMPARTMENT, VAULT)
        assert statement.count("{") == statement.count("}") == 1


class TestPolicyIsOptional:
    """Both OCIDs are needed, and neither alone is usable."""

    def test_absent_without_a_vault(self):
        # The only configuration in which the policy is skipped. Config requires
        # tenancy-id alongside vault-id, so a vault without a tenancy cannot
        # reach this function at all.
        assert identity.create_vault_read_policy(build_config()) is None

    def test_both_are_required_together(self):
        with pytest.raises(ValueError, match="Set vault-id"):
            build_config(tenancy_id="ocid1.tenancy.oc1..abc")
        with pytest.raises(ValueError, match="Set tenancy-id"):
            build_config(vault_id=VAULT)
