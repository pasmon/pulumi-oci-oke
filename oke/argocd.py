"""Bootstrap Argo CD and the Argo CD repository credential.

Argo CD is never exposed by this program. The release is a plain ClusterIP and
the UI is reached with a port-forward against the generated kubeconfig.
"""

import re

import pulumi
import pulumi_kubernetes as k8s

ARGOCD_NAMESPACE = "argocd"
ARGOCD_HELM_REPO = "https://argoproj.github.io/argo-helm"
ARGOCD_HELM_CHART = "argo-cd"
ARGOCD_HELM_VERSION = "8.3.3"


def build_repository_secret_string_data(
    repo_url,
    repo_username=None,
    repo_password=None,
    repo_ssh_private_key=None,
    github_app_auth=None,
):
    """Build the optional Argo CD repository secret payload.

    Exactly one authentication method is allowed. The GitHub App credentials
    must all be present or all absent.
    """
    is_ssh_repo_url = repo_url.startswith("ssh://") or re.match(
        r"^[^/@:\s]+@[^:/\s]+:.+$", repo_url
    )
    github_app_auth = github_app_auth or {}
    github_app_id = github_app_auth.get("id")
    github_app_installation_id = github_app_auth.get("installation_id")
    github_app_private_key = github_app_auth.get("private_key")

    has_https_auth = repo_username is not None or repo_password is not None
    has_ssh_auth = repo_ssh_private_key is not None
    has_github_app_auth = (
        github_app_id is not None
        or github_app_installation_id is not None
        or github_app_private_key is not None
    )

    if sum((has_https_auth, has_ssh_auth, has_github_app_auth)) > 1:
        raise ValueError(
            "Use only one Argo CD repository authentication method: "
            "HTTPS credentials, an SSH private key, or GitHub App credentials."
        )
    if has_https_auth and (repo_username is None or repo_password is None):
        raise ValueError(
            "Set both argocd-repo-username and argocd-repo-password, or neither."
        )
    if has_github_app_auth and (
        github_app_id is None
        or github_app_installation_id is None
        or github_app_private_key is None
    ):
        raise ValueError(
            "Set argocd-github-app-id, argocd-github-app-installation-id, "
            "and argocd-github-app-private-key together, or omit them all."
        )
    if is_ssh_repo_url and (has_https_auth or has_github_app_auth):
        raise ValueError(
            "Use SSH repository URLs only with argocd-repo-ssh-private-key."
        )
    if not is_ssh_repo_url and has_ssh_auth:
        raise ValueError(
            "Use argocd-repo-ssh-private-key only with ssh:// or SCP-style SSH repository URLs."
        )

    if repo_ssh_private_key is not None:
        return {
            "type": "git",
            "url": repo_url,
            "sshPrivateKey": repo_ssh_private_key,
        }
    if has_github_app_auth:
        return {
            "type": "git",
            "url": repo_url,
            "githubAppID": github_app_id,
            "githubAppInstallationID": github_app_installation_id,
            "githubAppPrivateKey": github_app_private_key,
        }
    if repo_username is not None and repo_password is not None:
        return {
            "type": "git",
            "url": repo_url,
            "username": repo_username,
            "password": repo_password,
        }
    return None


def create_argocd(cfg, kubeconfig):
    """Install Argo CD and seed the bootstrap Application.

    Args:
        cfg: the validated :class:`oke.config.Config`.
        kubeconfig: the OKE kubeconfig, used to build the Kubernetes provider.

    Returns:
        A dict with the resources that ``__main__`` exports.
    """
    provider = k8s.Provider(
        "oke-kubernetes",
        kubeconfig=kubeconfig,
        enable_server_side_apply=True,
    )

    argocd_namespace = k8s.core.v1.Namespace(
        "argocd-namespace",
        metadata={"name": ARGOCD_NAMESPACE},
        opts=pulumi.ResourceOptions(provider=provider),
    )

    # When argocd-managed-by-pulumi is false the release is intentionally not
    # created, so that the argocd-self Application in gitops/bootstrap becomes
    # the sole owner of the Argo CD release.
    argocd_release = None
    if cfg.argocd_managed_by_pulumi:
        argocd_release = k8s.helm.v3.Release(
            "argocd",
            chart=ARGOCD_HELM_CHART,
            version=ARGOCD_HELM_VERSION,
            namespace=ARGOCD_NAMESPACE,
            repository_opts=k8s.helm.v3.RepositoryOptsArgs(repo=ARGOCD_HELM_REPO),
            values={
                "crds": {"install": True},
                # Deliberately ClusterIP. The UI is reached by port-forward.
                "server": {"service": {"type": "ClusterIP"}},
            },
            opts=pulumi.ResourceOptions(
                provider=provider, depends_on=[argocd_namespace]
            ),
        )

    repository_secret = None
    secret_data = build_repository_secret_string_data(
        repo_url=cfg.argocd_repo_url,
        repo_username=cfg.argocd_repo_username,
        repo_password=cfg.argocd_repo_password,
        repo_ssh_private_key=cfg.argocd_repo_ssh_private_key,
        github_app_auth={
            "id": cfg.argocd_github_app_id,
            "installation_id": cfg.argocd_github_app_installation_id,
            "private_key": cfg.argocd_github_app_private_key,
        },
    )
    if secret_data is not None:
        repository_secret = k8s.core.v1.Secret(
            "argocd-bootstrap-repo",
            metadata={
                "name": "bootstrap-repo",
                "namespace": ARGOCD_NAMESPACE,
                "labels": {"argocd.argoproj.io/secret-type": "repository"},
            },
            string_data=secret_data,
            type="Opaque",
            opts=pulumi.ResourceOptions(
                provider=provider, depends_on=[argocd_namespace]
            ),
        )

    root_dependencies = []
    if argocd_release is not None:
        root_dependencies.append(argocd_release)
    if repository_secret is not None:
        root_dependencies.append(repository_secret)

    bootstrap_application = k8s.apiextensions.CustomResource(
        "bootstrap-root-application",
        api_version="argoproj.io/v1alpha1",
        kind="Application",
        metadata={"name": "bootstrap-root", "namespace": ARGOCD_NAMESPACE},
        spec={
            "project": "default",
            "source": {
                "repoURL": cfg.argocd_repo_url,
                "targetRevision": cfg.argocd_repo_target_revision,
                "path": cfg.argocd_repo_path,
            },
            "destination": {
                "server": "https://kubernetes.default.svc",
                "namespace": ARGOCD_NAMESPACE,
            },
            "syncPolicy": {
                "automated": {"prune": True, "selfHeal": True},
                "syncOptions": ["CreateNamespace=true"],
            },
        },
        opts=pulumi.ResourceOptions(
            provider=provider, depends_on=root_dependencies or [argocd_namespace]
        ),
    )

    return {
        "provider": provider,
        "namespace": argocd_namespace,
        "release": argocd_release,
        "repository_secret": repository_secret,
        "bootstrap_application": bootstrap_application,
    }
