"""Install Argo CD and seed the single Application that starts GitOps.

This module is deliberately the whole of Pulumi's involvement with Argo CD. It
creates the namespace, the Helm release, an optional repository credential and
one root ``Application``, and nothing else. Every further object, including Argo
CD's own Helm release once it is handed over, belongs to the GitOps repository.

Two things follow from that, and both are load-bearing:

* The root Application uses the ``default`` project. Argo CD creates that project
  itself, fully permissive, so a bootstrap needs no pre-created AppProject. Any
  named project would have to exist before the Application that defines it.
* The source is a directory, not a single file. The GitOps repository holds one
  Application per category, and pointing at a file would mean hardcoding one of
  them here.

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


def bootstrap_application_spec(cfg):
    """Build the spec of the one Application that hands over to GitOps.

    Kept separate from the resource so its shape can be asserted in a unit test
    without a Pulumi engine. The three properties that make the handover minimal
    are all visible here: one project, one path, one directory.
    """
    return {
        # `default` is the only project that exists at this point. Argo CD
        # creates it permissive, and no named project could be referenced by
        # the very Application that defines it.
        "project": "default",
        "source": {
            "repoURL": cfg.argocd_repo_url,
            "targetRevision": cfg.argocd_repo_target_revision,
            "path": cfg.argocd_repo_path,
            # The GitOps repo's entrypoint is a directory of Applications, one
            # per category. Recursing is what makes that a single seed.
            "directory": {"recurse": True},
        },
        "destination": {
            "server": "https://kubernetes.default.svc",
            "namespace": ARGOCD_NAMESPACE,
        },
        "syncPolicy": {
            "automated": {"prune": True, "selfHeal": True},
            "syncOptions": ["CreateNamespace=true"],
        },
    }


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
            # retain_on_delete is what makes the hand-over clean. Setting
            # argocd-managed-by-pulumi to false drops this resource from the
            # program, and without this flag Pulumi would delete it, which for a
            # Helm release means running `helm uninstall` against a live
            # cluster. Uninstalling Argo CD's own release takes its ConfigMaps,
            # Secrets and ServiceAccounts with it, and Argo CD cannot recreate
            # them on its own: the application controller reads argocd-cm during
            # startup and exits fatally when it is missing, and it loses the
            # ServiceAccount it would use to recreate anything. The cluster
            # dead-ends with every Application stuck at Unknown until the
            # objects are recreated by hand.
            #
            # With retain_on_delete the release is simply forgotten. Pulumi stops
            # tracking it, the running release is untouched, and
            # argo-apps/core-apps/argo-cd.yaml adopts the live objects on its
            # next sync, which is what the hand-over is actually for. A full
            # `pulumi destroy` still removes the cluster and everything in it.
            opts=pulumi.ResourceOptions(
                provider=provider,
                depends_on=[argocd_namespace],
                retain_on_delete=True,
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
        spec=bootstrap_application_spec(cfg),
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
