"""Unit tests for the Argo CD bootstrap and the repository credential."""

import pytest

from oke import argocd

HTTPS_URL = "https://github.com/pasmon/pulumi-oci-oke.git"
SSH_URL = "ssh://git@github.com/pasmon/pulumi-oci-oke.git"


class TestChartConstants:
    """Pinned chart coordinates match the gitops manifests."""

    def test_chart_and_version(self):
        assert argocd.ARGOCD_HELM_CHART == "argo-cd"
        assert argocd.ARGOCD_HELM_VERSION == "8.3.3"

    def test_repo_matches_the_argocd_self_application(self):
        assert argocd.ARGOCD_HELM_REPO == "https://argoproj.github.io/argo-helm"

    def test_namespace(self):
        assert argocd.ARGOCD_NAMESPACE == "argocd"


class TestNoAuthentication:
    """A public repository needs no credential."""

    def test_none_is_returned(self):
        assert argocd.build_repository_secret_string_data(HTTPS_URL) is None


class TestHttpsAuthentication:
    """Username and password must be supplied together."""

    def test_both_produce_a_secret(self):
        result = argocd.build_repository_secret_string_data(
            HTTPS_URL, repo_username="git", repo_password="token"
        )
        assert result == {
            "type": "git",
            "url": HTTPS_URL,
            "username": "git",
            "password": "token",
        }

    def test_username_alone_is_rejected(self):
        with pytest.raises(
            ValueError, match="both argocd-repo-username and argocd-repo-password"
        ):
            argocd.build_repository_secret_string_data(HTTPS_URL, repo_username="git")

    def test_password_alone_is_rejected(self):
        with pytest.raises(
            ValueError, match="both argocd-repo-username and argocd-repo-password"
        ):
            argocd.build_repository_secret_string_data(HTTPS_URL, repo_password="token")


class TestSshAuthentication:
    """SSH keys only pair with SSH URLs."""

    def test_ssh_key_produces_a_secret(self):
        result = argocd.build_repository_secret_string_data(
            SSH_URL, repo_ssh_private_key="-----BEGIN KEY-----"
        )
        assert result["sshPrivateKey"] == "-----BEGIN KEY-----"
        assert result["url"] == SSH_URL

    def test_ssh_key_with_https_url_is_rejected(self):
        with pytest.raises(ValueError, match="only with ssh://"):
            argocd.build_repository_secret_string_data(
                HTTPS_URL, repo_ssh_private_key="-----BEGIN KEY-----"
            )

    def test_ssh_url_with_https_credentials_is_rejected(self):
        with pytest.raises(ValueError, match="only with argocd-repo-ssh-private-key"):
            argocd.build_repository_secret_string_data(
                SSH_URL, repo_username="git", repo_password="token"
            )

    def test_scp_style_url_is_recognised(self):
        result = argocd.build_repository_secret_string_data(
            "git@github.com:pasmon/pulumi-oci-oke.git", repo_ssh_private_key="key"
        )
        assert result["type"] == "git"


class TestGithubAppAuthentication:
    """All three GitHub App fields are required together."""

    def test_complete_auth_produces_a_secret(self):
        result = argocd.build_repository_secret_string_data(
            HTTPS_URL,
            github_app_auth={
                "id": "12345",
                "installation_id": "67890",
                "private_key": "-----BEGIN PRIVATE KEY-----",
            },
        )
        assert result["githubAppID"] == "12345"
        assert result["githubAppInstallationID"] == "67890"
        assert result["githubAppPrivateKey"] == "-----BEGIN PRIVATE KEY-----"

    def test_partial_auth_is_rejected(self):
        with pytest.raises(ValueError, match="together, or omit them all"):
            argocd.build_repository_secret_string_data(
                HTTPS_URL, github_app_auth={"id": "12345"}
            )


class TestMutualExclusion:
    """Only one authentication method at a time."""

    def test_https_plus_ssh_is_rejected(self):
        with pytest.raises(
            ValueError, match="Use only one Argo CD repository authentication method"
        ):
            argocd.build_repository_secret_string_data(
                HTTPS_URL,
                repo_username="git",
                repo_password="token",
                repo_ssh_private_key="key",
            )

    def test_https_plus_github_app_is_rejected(self):
        with pytest.raises(
            ValueError, match="Use only one Argo CD repository authentication method"
        ):
            argocd.build_repository_secret_string_data(
                HTTPS_URL,
                repo_username="git",
                repo_password="token",
                github_app_auth={"id": "1", "installation_id": "2", "private_key": "k"},
            )
