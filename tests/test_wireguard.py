"""Unit tests for the Wireguard tunnel script and address derivation."""

import asyncio
import base64
import os
import shutil
import subprocess

import pulumi
import pytest

from oke import wireguard
from tests.conftest import build_config

WIREGUARD_VALUES = {
    "wireguard_peer_endpoint": "198.51.100.7",
    "wireguard_peer_public_key": "peerPublicKey=",
    "wireguard_preshared_key": "presharedKey=",
}


def wg_config(**overrides):
    """Build a config with Wireguard enabled."""
    values = dict(WIREGUARD_VALUES)
    values.update(overrides)
    return build_config(**values)


def secret_config():
    """Build a config with the preshared key as a secret Output."""
    asyncio.set_event_loop(asyncio.new_event_loop())
    return wg_config(
        wireguard_preshared_key=pulumi.Output.from_input("realPresharedKey="),
    )


def decode_payload(payload):
    """Resolve and base64-decode node user data, which may be an Output."""
    if isinstance(payload, pulumi.Output):
        payload = asyncio.get_event_loop().run_until_complete(payload.future())
    return base64.b64decode(payload).decode("utf-8")


@pytest.mark.skipif(os.name == "nt", reason="Shell execution requires Unix permissions")
class TestNodeKeyLifecycle:
    """Execute the rendered key setup without touching host configuration."""

    @staticmethod
    def run_setup(directory, fail_generation=False):
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("bash is required for bootstrap execution tests")
        decoded = decode_payload(wireguard.build_node_user_data(wg_config()))
        setup = decoded[
            decoded.index("umask 077") : decoded.index("install -m 644 /dev/null")
        ].replace("/etc/wireguard", directory.as_posix())
        mock = """
wg() {
  case "$1" in
    genkey) printf 'private-%s\\n' "$BASHPID" ;;
    pubkey)
      read -r key
      case "$key" in
        private-*) printf 'public-%s\\n' "${key#private-}" ;;
        *) return 1 ;;
      esac ;;
  esac
}
"""
        if fail_generation:
            mock = "wg() { return 1; }\n"
        script = (
            "set -eu\nPRIVATE_IP=10.10.1.36\nTUNNEL_BASE=10.99.0\nLAST_OCTET=36\n"
            + mock
            + setup
        )
        return subprocess.run(  # pylint: disable=subprocess-run-check
            [bash, "-s"], input=script, text=True, capture_output=True, check=False
        )

    def test_independent_nodes_and_repeated_bootstrap(self, tmp_path):
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        initial = self.run_setup(first)
        assert initial.returncode == 0, initial.stderr
        key = (first / "wg0-node.key").read_text()
        repeated = self.run_setup(first)
        other = self.run_setup(second)
        assert repeated.returncode == other.returncode == 0
        assert repeated.stdout == initial.stdout
        assert (first / "wg0-node.key").read_text() == key
        assert (second / "wg0-node.key").read_text() != key
        assert (first / "wg0-node.key").stat().st_mode & 0o777 == 0o600
        assert key.strip() not in initial.stdout

    def test_generation_failure_does_not_leave_a_key(self, tmp_path):
        result = self.run_setup(tmp_path, fail_generation=True)
        assert result.returncode == 0
        assert "WARNING: node key generation failed" in result.stderr
        assert not list(tmp_path.iterdir())
        assert "WireGuard enrollment:" not in result.stdout

    def test_invalid_key_is_not_overwritten(self, tmp_path):
        key = tmp_path / "wg0-node.key"
        key.write_text("invalid\n")
        result = self.run_setup(tmp_path)
        assert result.returncode == 0
        assert "WARNING: invalid node key" in result.stderr
        assert key.read_text() == "invalid\n"
        assert "WireGuard enrollment:" not in result.stdout

    def test_symlink_is_rejected(self, tmp_path):
        target = tmp_path / "target"
        target.write_text("private-test\n")
        (tmp_path / "wg0-node.key").symlink_to(target)
        result = self.run_setup(tmp_path)
        assert result.returncode == 0
        assert "WARNING: node key must not be a symlink" in result.stderr
        assert target.read_text() == "private-test\n"


class TestNodeTunnelAddress:
    """Each node derives a unique address from its own private IP."""

    def test_address_is_derived_from_the_private_ip_octet(self):
        address = wireguard.node_tunnel_address("10.10.1.11", "10.99.0.0/24")
        assert address == "10.99.0.11/24"

    def test_two_nodes_get_different_addresses(self):
        first = wireguard.node_tunnel_address("10.10.1.11", "10.99.0.0/24")
        second = wireguard.node_tunnel_address("10.10.1.12", "10.99.0.0/24")
        assert first != second
        assert first == "10.99.0.11/24"
        assert second == "10.99.0.12/24"

    def test_address_stays_inside_the_tunnel_subnet(self):
        for octet in (2, 10, 11, 12, 100, 254):
            address = wireguard.node_tunnel_address(f"10.10.1.{octet}", "10.99.0.0/24")
            assert address.startswith("10.99.0.")

    def test_derived_address_never_collides_with_the_router(self):
        # The router takes .1, so a node with private IP ending in .1 is a clash.
        with pytest.raises(ValueError, match="collides with the router address"):
            wireguard.node_tunnel_address("10.10.1.1", "10.99.0.0/24")

    def test_address_outside_a_narrow_subnet_is_rejected(self):
        with pytest.raises(ValueError, match="outside the tunnel subnet"):
            wireguard.node_tunnel_address("10.10.1.200", "10.99.0.0/28")

    def test_prefix_length_is_preserved(self):
        address = wireguard.node_tunnel_address("10.10.1.11", "10.99.0.0/24")
        assert address.endswith("/24")


class TestAllowedIps:
    """AllowedIPs is the tunnel subnet plus any routed LAN prefixes."""

    def test_subnet_only(self):
        cfg = wg_config()
        assert wireguard.wireguard_allowed_ips(cfg) == "10.99.0.0/24"

    def test_routed_prefixes_follow_the_subnet(self):
        cfg = wg_config(
            wireguard_allowed_cidrs=["192.168.88.0/24", "192.168.88.200/32"]
        )
        assert (
            wireguard.wireguard_allowed_ips(cfg)
            == "10.99.0.0/24, 192.168.88.0/24, 192.168.88.200/32"
        )


class TestNodeUserData:
    """User data is only produced when Wireguard is configured."""

    def test_none_when_disabled(self):
        assert wireguard.build_node_user_data(build_config()) is None

    def test_base64_encoded_when_enabled(self):
        payload = wireguard.build_node_user_data(wg_config())
        decoded = base64.b64decode(payload).decode("utf-8")
        assert "wg-quick@wg0" in decoded

    def test_script_uses_the_pod_cidr_for_masquerading(self):
        payload = wireguard.build_node_user_data(wg_config(pods_cidr="10.244.0.0/16"))
        decoded = base64.b64decode(payload).decode("utf-8")
        assert "10.244.0.0/16" in decoded
        # The RKE2 pod range must not leak into the OKE script.
        assert "10.42.0.0/16" not in decoded

    def test_script_discovers_its_own_private_ip(self):
        # Both nodes share identical user data, so the address cannot be baked in.
        payload = wireguard.build_node_user_data(wg_config())
        decoded = base64.b64decode(payload).decode("utf-8")
        assert "169.254.169.254" in decoded
        assert "LAST_OCTET" in decoded

    def test_script_carries_the_peer_endpoint_and_port(self):
        payload = wireguard.build_node_user_data(wg_config())
        decoded = base64.b64decode(payload).decode("utf-8")
        assert "Endpoint = 198.51.100.7:51820" in decoded

    def test_script_resolves_the_secret_preshared_key(self):
        decoded = decode_payload(wireguard.build_node_user_data(secret_config()))
        assert "PrivateKey = $NODE_PRIVATE_KEY" in decoded
        assert "PresharedKey = realPresharedKey=" in decoded
        assert "Calling __str__" not in decoded

    def test_node_key_is_generated_locally_and_reused(self):
        decoded = decode_payload(wireguard.build_node_user_data(wg_config()))
        assert "NODE_KEY=/etc/wireguard/wg0-node.key" in decoded
        assert 'if [ ! -e "$NODE_KEY" ]; then' in decoded
        assert 'wg genkey > "$KEY_TEMP"' in decoded
        assert 'mv "$KEY_TEMP" "$NODE_KEY"' in decoded
        assert "wireguard-private-key" not in decoded

    def test_node_key_is_protected_and_validated(self):
        decoded = decode_payload(wireguard.build_node_user_data(wg_config()))
        assert 'if [ -L "$NODE_KEY" ]; then' in decoded
        assert decoded.index("umask 077") < decoded.index("KEY_TEMP=$(mktemp")
        assert 'chmod 600 "$NODE_KEY"' in decoded
        assert 'if ! NODE_PUBLIC_KEY=$(wg pubkey < "$NODE_KEY"); then' in decoded
        assert 'echo "WARNING: node key generation failed' in decoded
        assert 'echo "WARNING: invalid node key' in decoded

    def test_enrollment_logs_only_public_key_and_host_address(self):
        decoded = decode_payload(wireguard.build_node_user_data(wg_config()))
        enrollment = next(
            line
            for line in decoded.splitlines()
            if line.startswith('echo "WireGuard enrollment:')
        )
        assert "node=$PRIVATE_IP" in enrollment
        assert "address=$TUNNEL_BASE.$LAST_OCTET/32" in enrollment
        assert "public-key=$NODE_PUBLIC_KEY" in enrollment
        assert "NODE_PRIVATE_KEY" not in enrollment

    def test_legacy_shared_private_key_is_not_embedded(self):
        cfg = wg_config(wireguard_private_key="legacySharedKey=")
        decoded = decode_payload(wireguard.build_node_user_data(cfg))
        assert "legacySharedKey=" not in decoded

    def test_script_starts_with_a_shebang(self):
        # cloud-init picks the content type from the shebang. Without one it
        # reports text/x-not-multipart and never runs the script at all, which
        # is indistinguishable from the script working.
        payload = wireguard.build_node_user_data(wg_config())
        decoded = base64.b64decode(payload).decode("utf-8")
        assert decoded.startswith("#!/bin/bash\n")

    def test_payload_is_base64_because_the_compute_api_requires_it(self):
        # OCI rejects instance metadata whose user_data is not base64 with
        # "user_data must be base64 encoded".
        payload = wireguard.build_node_user_data(wg_config())
        assert payload == base64.b64encode(base64.b64decode(payload)).decode("utf-8")
        assert base64.b64decode(payload).decode("utf-8").startswith("#!")

    def test_script_reads_the_vnics_endpoint(self):
        # /opc/v2/vnic/ is singular and answers 404. The empty PRIVATE_IP then
        # failed `test -n` under `set -eu`, aborting user data on its first real
        # line, so no node ever got a tunnel and none ever joined the cluster.
        payload = wireguard.build_node_user_data(wg_config())
        decoded = base64.b64decode(payload).decode("utf-8")
        assert "/opc/v2/vnics/" in decoded
        assert not [
            line
            for line in decoded.splitlines()
            if "/opc/v2/vnic/" in line and not line.lstrip().startswith("#")
        ], "a comment may name the singular path, a command may not"

    def test_a_missing_private_ip_warns_instead_of_failing_bootstrap(self):
        payload = wireguard.build_node_user_data(wg_config())
        decoded = base64.b64decode(payload).decode("utf-8")
        assert (
            'test -n "$PRIVATE_IP" || {' in decoded
        ), "an unreadable metadata service must not abort user data"

    def test_a_tunnel_that_will_not_start_warns_instead_of_failing_bootstrap(self):
        payload = wireguard.build_node_user_data(wg_config())
        decoded = base64.b64decode(payload).decode("utf-8")
        assert (
            "systemctl restart wg-quick@wg0 \\\n"
            '  || echo "WARNING: wg-quick failed to start' in decoded
        ), "a rejected key or unreachable peer must not keep the node unregistered"

    def test_script_installs_the_interface(self):
        payload = wireguard.build_node_user_data(wg_config())
        decoded = base64.b64decode(payload).decode("utf-8")
        assert "systemctl enable wg-quick@wg0" in decoded
        assert "PersistentKeepalive = 25" in decoded

    def test_script_installs_with_dnf_because_the_image_is_oracle_linux(self):
        # apt-get does not exist on the OL8 node pool image, and a user data
        # script that fails under `set -eu` keeps the node out of the cluster.
        payload = wireguard.build_node_user_data(wg_config())
        decoded = base64.b64decode(payload).decode("utf-8")
        assert "dnf install -y wireguard-tools" in decoded
        assert not [
            line for line in decoded.splitlines() if line.startswith("apt-get")
        ], "a comment may mention apt-get, a command may not"

    def test_a_missing_package_does_not_fail_node_bootstrap(self):
        payload = wireguard.build_node_user_data(wg_config())
        decoded = base64.b64decode(payload).decode("utf-8")
        assert (
            "dnf install -y wireguard-tools || {" in decoded
        ), "the install must not abort the script on failure"

    def test_the_interface_check_is_bounded_and_warns_instead_of_aborting(self):
        payload = wireguard.build_node_user_data(wg_config())
        decoded = base64.b64decode(payload).decode("utf-8")
        assert (
            "timeout 60 systemctl is-active --wait wg-quick@wg0 \\\n"
            "  || echo" in decoded
        ), "an unbounded is-active --wait can hang user data forever"

    def test_rules_live_in_posthooks_so_they_survive_an_iptables_flush(self):
        payload = wireguard.build_node_user_data(wg_config())
        decoded = base64.b64decode(payload).decode("utf-8")
        assert "PostUp = iptables" in decoded
        assert "PostDown = iptables" in decoded


class TestOkeBootstrap:
    """Custom user data replaces OKE's default, so it must run the bootstrap."""

    @staticmethod
    def commands(decoded):
        return [
            line
            for line in decoded.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    def test_script_fetches_and_runs_the_oke_init_script(self):
        # Without this, the node boots, the script exits 0, and kubelet is
        # never configured, so the node never registers with the cluster.
        decoded = decode_payload(wireguard.build_node_user_data(wg_config()))
        assert wireguard.OKE_INIT_SCRIPT_URL in decoded
        assert wireguard.OKE_INIT_SCRIPT_URL.endswith(
            "/opc/v2/instance/metadata/oke_init_script"
        )
        assert "base64 --decode > /var/run/oke-init.sh" in decoded
        assert "bash /var/run/oke-init.sh" in self.commands(decoded)

    def test_bootstrap_fails_loudly_on_a_bad_fetch(self):
        decoded = decode_payload(wireguard.build_node_user_data(wg_config()))
        fetch = next(
            line for line in self.commands(decoded) if line.startswith("curl --fail")
        )
        assert "Authorization: Bearer Oracle" in fetch

    def test_bootstrap_runs_before_any_early_exit(self):
        # The tunnel steps exit 0 when they cannot proceed. If any of them ran
        # first, it would skip node registration.
        commands = self.commands(
            decode_payload(wireguard.build_node_user_data(wg_config()))
        )
        bootstrap = commands.index("bash /var/run/oke-init.sh")
        first_exit = next(
            index for index, line in enumerate(commands) if "exit 0" in line
        )
        first_tunnel_step = next(
            index for index, line in enumerate(commands) if "PRIVATE_IP=" in line
        )
        assert bootstrap < first_tunnel_step < first_exit

    def test_bootstrap_is_present_when_the_keys_are_secret(self):
        decoded = decode_payload(wireguard.build_node_user_data(secret_config()))
        assert "bash /var/run/oke-init.sh" in self.commands(decoded)
