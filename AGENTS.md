# Repository guide for agents

## What this repository does

Provisions an OCI managed Kubernetes cluster with two ARM nodes and hands over to
Argo CD. Read `README.md` first, especially the cost section.

## The two invariants

Break either of these and the design stops working.

**1. One `pulumi up` must be sufficient.** Pulumi may not create, or depend on,
any object whose CRD is installed by Argo CD. Every Pulumi resource is either an
OCI resource or one of `Namespace`, `Secret`, `helm.sh/v3 Release`,
`argoproj.io/v1alpha1 Application`. If you find yourself wanting to `kubectl
wait` for a CRD, the resource belongs in `argo-apps` instead.

**2. Pulumi creates no application secret.** Credentials live in OCI Vault and
enter the cluster only through External Secrets Operator, authenticated as an
instance principal. Pulumi's only involvement is creating empty namespaces. The
single exception is the optional Argo CD repository credential for a private git
remote, which is inherent to bootstrapping GitOps.

`tests/test_namespaces.py` enforces the second one as an assertion rather than a
comment.

**3. The hand-over is one Application and there is no `gitops/` tree.** Pulumi
seeds `bootstrap-root` at `argocd-repo-path` in `argo-apps`, with
`directory: recurse`, and that is the whole of the GitOps involvement. It uses
the `default` project, because Argo CD creates that project itself and a named
one would have to exist before the Application that defines it. Adding a second
seed Application, an `AppProject`, or a manifest directory to sync is a
regression. `tests/test_program_wiring.py` asserts the absence.

## Layout

| Path | Purpose |
|---|---|
| `__main__.py` | Orchestration only. Calls the modules, then exports. |
| `oke/config.py` | Loads and validates all stack config. No resources. |
| `oke/networking.py` | VCN, gateways, route table, security lists, subnets |
| `oke/cluster.py` | Availability domain and image discovery, cluster, node pool |
| `oke/identity.py` | The IAM policy granting nodes read on the vault |
| `oke/kubeconfig.py` | Fetches and writes `out/oke_kubeconfig` |
| `oke/argocd.py` | Argo CD Helm release and the single bootstrap Application |
| `oke/namespaces.py` | The three namespaces GitOps writes into |
| `oke/wireguard.py` | Optional tunnel script for OKE nodes |

## Commands

```bash
uv sync --locked
pulumi preview        # validation happens here, not mid-apply
pulumi up
export KUBECONFIG=out/oke_kubeconfig
```

Checks, all of which must pass before committing:

```bash
uv run python -m compileall -q __main__.py oke tests
uv run ruff check .
uv run black --check .
uv run pylint __main__.py oke tests
uv run bandit -c pyproject.toml -r . -x ./tests -x ./.venv
uv run pytest -v
```

## Conventions

- Configuration failures raise in `oke/config.py` so they surface at
  `pulumi preview`, never part-way through an apply.
- Prefer discovering values from OCI over hardcoding them. The availability
  domains, the Kubernetes version and the node image are all resolved at plan
  time. The sibling `pulumi-oci-rke` hardcodes `Dtqv:EU-STOCKHOLM-1-AD-1`; do not
  copy that.
- Cost warnings print rather than raise. Exceeding Always Free is a deliberate
  choice the operator makes, not a programming error. See README.
- Comments explain why, not what. The two-subnet topology, the single-owner rule
  and the tunnel address derivation all have non-obvious reasons; keep those
  comments when editing the surrounding code.

## Things that are easy to get wrong

- **Node sizing.** Defaults are 4 OCPU / 24 GB total, which is twice the Always
  Free allowance. It requires PAYG. Do not "fix" this by silently changing the
  default.
- **Pod CIDR must not overlap the VCN.** OKE routes pod traffic over the VNICs, so
  a pod range inside the VCN collides with node addresses. `config.py` rejects it.
- **The node subnet is public on purpose.** See README, "Why the node subnet is
  public". Making it private reintroduces the two-stage apply.
- **Wireguard addresses are derived per node.** Both nodes run identical
  user-data, so a fixed address would collide. See `oke/wireguard.py`.
- **ESO authenticates as an instance principal, not Workload Identity.** OKE only
  issues workload identity tokens on *enhanced* clusters, which are billed hourly;
  this program creates a basic cluster to stay in Always Free. Do not reintroduce
  `principalType: Workload` or a `DynamicGroup` here without changing the cluster
  type first, and read README, "Why instance principals" for the trade-off.
- **The IAM policy lives in the tenancy root**, not in the configured
  compartment. IAM resources are tenancy-scoped. The *statement* names the
  compartment holding the vault, which is a different thing.
- **Do not create the `oke` dynamic group.** OCI creates one in every tenancy and
  it already matches the cluster's node instances. A Pulumi-managed group of that
  name collides with it.
- **The policy must be `read` and scoped by `target.vault.id`.** `manage` grants
  every pod on a node write access; omitting the vault scope grants every vault
  in the compartment. Both are regressions the instance-principal trade depends on
  not happening.
- **The chart version is pinned in two repositories.** `ARGOCD_HELM_VERSION` here
  and `core-apps/argo-cd.yaml` in `argo-apps` describe one Helm release. They
  must match, or handing over downgrades Argo CD under the running cluster.

## Neighbouring repositories

| Repository | Relationship |
|---|---|
| `pulumi-oci-rke` | The predecessor. Self-managed RKE2 on VMs. Destroy after cutover. |
| `argo-apps` | Owns everything Argo CD deploys, including the Gateway, the ClusterIssuer and the cache PVC. |
| `radio-frontend` and friends | The workloads. Their charts are not edited to solve cluster problems. |

Do not modify `argo-apps` or the workload charts to work around something in this
repository. For example, the shared `radio-audio-cache` volume needs the two
consumers on one node, because OCI block volumes are single-attach. That is
solved in `argo-apps` with required pod affinity in
`apps/values/radio-audio-gateway.yaml`, on the gateway only. Two details are
easy to get backwards: a `PersistentVolumeClaim` has no `nodeAffinity` field, so
one set on a claim is discarded by the API server and the claim cannot carry the
constraint; and the gateway and worker charts *do* expose `affinity`, which is
why their pinned releases cannot move back to the versions that lacked it.

## Destroying

```bash
pulumi destroy
```

The IAM policy and the VCN are both removed. Vault secrets are untouched, since
Pulumi never owned them.
