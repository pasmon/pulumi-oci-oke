"""Shared fixtures for the Pulumi OKE provisioning tests.

Two kinds of fixture live here:

* :func:`build_config`, which validates configuration in isolation with no
  Pulumi engine involved.
* The ``*_stack`` fixtures, which load ``__main__.py`` under mocked OCI and
  Kubernetes providers so wiring mistakes surface in tests rather than in
  ``pulumi preview``.
"""

import asyncio
import importlib.util
import json
import os
import shutil
import tempfile

import pulumi
import pytest

from oke.config import Config

PROJECT = "oci-oke-provision"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROGRAM_PATH = os.path.join(REPO_ROOT, "__main__.py")


class FakeConfig:
    """Stand-in for ``pulumi.Config`` that returns values from a dict.

    ``require`` mirrors the Pulumi behaviour of raising on a missing key, so
    the tests exercise the same failure modes as a real ``pulumi preview``.
    """

    def __init__(self, values):
        self._values = values

    def get(self, key):
        return self._values.get(key)

    def require(self, key):
        if key not in self._values:
            raise KeyError(key)
        return self._values[key]

    def get_secret(self, key):
        return self._values.get(key)

    def get_object(self, key):
        return self._values.get(key)


BASE_VALUES = {
    "compartment-id": "ocid1.compartment.oc1..test",
    "ssh-public-key-path": "unused-in-tests",
    "argocd-repo-url": "https://github.com/pasmon/pulumi-oci-oke.git",
}


def build_config(**overrides):
    """Build a :class:`oke.config.Config` from the base values plus overrides.

    Overrides are given with underscores and converted to the dashed keys that
    Pulumi config uses, so a test can say ``node_ocpus=1`` and it lands on
    ``node-ocpus``.
    """
    values = dict(BASE_VALUES)
    for key, value in overrides.items():
        values[key.replace("_", "-")] = value
    return Config(FakeConfig(values))


@pytest.fixture
def ssh_key(tmp_path):
    """Write a throwaway SSH public key and return its path."""
    path = tmp_path / "id_ed25519.pub"
    path.write_text(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample test@example\n", encoding="utf-8"
    )
    return str(path)


# --------------------------------------------------------------------------- #
# Engine-mocked stack loading
# --------------------------------------------------------------------------- #


class StackMocks(pulumi.runtime.Mocks):
    """Mocks for the OCI data sources the program discovers from."""

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        outputs = dict(args.inputs)

        if args.typ == "oci:core/vcn:Vcn":
            outputs.setdefault("cidr_blocks", ["10.10.0.0/16"])
            outputs.setdefault("default_route_table_id", "ocid1.routetable.test")
        if args.typ == "oci:identity/dynamicgroup:DynamicGroup":
            outputs.setdefault("id", "ocid1.dynamicgroup.oc1..test")
        if args.typ == "oci:containerengine/cluster:Cluster":
            outputs.setdefault("name", "oke-cluster")

        return [args.name + "_id", outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):
        token = args.token

        if token == "oci:identity/getAvailabilityDomains:getAvailabilityDomains":
            return {
                "availability_domains": [
                    {"name": "Dtqv:EU-STOCKHOLM-1-AD-1"},
                    {"name": "qwuz:EU-STOCKHOLM-1-AD-2"},
                ],
                "compartment_id": "ocid1.tenancy.oc1..test",
            }

        if token == "oci:containerengine/getNodePoolOption:getNodePoolOption":
            return {
                "compartment_id": "ocid1.tenancy.oc1..test",
                "id": "all",
                "kubernetes_versions": ["v1.30.4", "v1.31.1", "v1.29.9"],
                "sources": [
                    {
                        "image_id": "ocid1.image.x86.test",
                        "source_name": "Oracle-Kubernetes-Engine-1.31.1-20260101",
                        "source_type": "IMAGE",
                    },
                    {
                        "image_id": "ocid1.image.arm.test",
                        "source_name": "Oracle-Kubernetes-Engine-aarch64-1.31.1-20260101",
                        "source_type": "IMAGE",
                    },
                ],
                "shapes": [{"shape": "VM.Standard.A1.Flex"}],
            }

        if token == "oci:containerengine/getClusterKubeConfig:getClusterKubeConfig":
            return {
                "cluster_id": "ocid1.cluster.oc1..test",
                "content": (
                    "apiVersion: v1\n"
                    "clusters:\n"
                    "- cluster:\n"
                    "    server: https://k8s.eu-stockholm-1.oke.test\n"
                    "  name: oke\n"
                    "contexts: []\n"
                    "current-context: oke\n"
                    "kind: Config\n"
                    "preferences: {}\n"
                    "users: []\n"
                ),
            }

        return {}


# Sizing stays inside Always Free so loading a stack does not print the cost
# banner. The overage path is covered directly in test_config.py instead.
BASE_STACK_CONFIG = {
    "compartment-id": "ocid1.tenancy.oc1..testtenancy0000000000000000000000000000000",
    "argocd-repo-url": "https://github.com/pasmon/pulumi-oci-oke.git",
    "node-count": "2",
    "node-ocpus": "1",
    "node-memory-gbs": "6",
}

WIREGUARD_STACK_CONFIG = {
    "wireguard-peer-endpoint": "198.51.100.7",
    "wireguard-peer-public-key": "peerPublicKey=",
    "wireguard-private-key": "privateKey=",
    "wireguard-preshared-key": "presharedKey=",
    "wireguard-allowed-cidrs": ["192.168.88.200/32"],
}

IDENTITY_STACK_CONFIG = {
    "tenancy-id": "ocid1.tenancy.oc1..testtenancy0000000000000000000000000000000",
    "vault-id": "ocid1.vault.oc1.eu-stockholm-1.aaaaaaaaexample",
}


def load_stack(stack_config):
    """Load __main__.py under mocks with the supplied configuration.

    Returns:
        The loaded module, which exposes every created resource as an attribute.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    temp_dir = tempfile.mkdtemp()
    ssh_pub_key_path = os.path.join(temp_dir, "id_ed25519.pub")
    with open(ssh_pub_key_path, "w", encoding="utf-8") as handle:
        handle.write("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample test@example\n")

    values = dict(stack_config)
    values["ssh-public-key-path"] = ssh_pub_key_path
    # Pulumi decodes object-typed config values from JSON strings.
    encoded = {
        f"{PROJECT}:{key}": value if isinstance(value, str) else json.dumps(value)
        for key, value in values.items()
    }
    os.environ["PULUMI_CONFIG"] = json.dumps(encoded)

    pulumi.runtime.set_mocks(StackMocks(), project=PROJECT, stack="test")

    spec = importlib.util.spec_from_file_location("oke_main", PROGRAM_PATH)
    program = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(program)
    program.__dict__["_temp_dir"] = temp_dir

    return program


def unload_stack(program):
    """Remove the files created while loading the stack."""
    shutil.rmtree(program.__dict__.get("_temp_dir", ""), ignore_errors=True)


@pytest.fixture(scope="session")
def pulumi_stack():
    """The program loaded with the base configuration."""
    program = load_stack(BASE_STACK_CONFIG)
    yield program
    unload_stack(program)


@pytest.fixture(scope="session")
def wireguard_stack():
    """The program loaded with the optional Wireguard tunnel enabled."""
    program = load_stack({**BASE_STACK_CONFIG, **WIREGUARD_STACK_CONFIG})
    yield program
    unload_stack(program)


@pytest.fixture(scope="session")
def identity_stack():
    """The program loaded with the Workload Identity identifiers configured."""
    program = load_stack({**BASE_STACK_CONFIG, **IDENTITY_STACK_CONFIG})
    yield program
    unload_stack(program)
