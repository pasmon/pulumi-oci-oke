# Repository guide for agents

## What this repository does

Provisions an OCI managed Kubernetes cluster with two ARM nodes and hands over to
Argo CD. Read `README.md` first, especially the cost section.

## The two invariants

Break either of these and the design stops working.

**1. One `pulumi up` must be sufficient.** Pulumi may not create, or depend on,
any object whose CRD is installed by Argo CD. Every Pulumi resource is either an
OCI resource or one of `Namespace`, `Secret`, `helm.sh/v3 Release`,
`argoproj.io/v1alpha1 Application`. If you find yourself wanting to `kubectl wait`
for a CRD, the resource belongs in `argo-apps` instead.

**2. Pulumi creates no application secret.** Credentials live in OCI Vault and
enter the cluster only through External Secrets Operator, authenticated by OKE
Workload Identity. Pulumi's only involvement is creating empty namespaces. The
single exception is the optional Argo CD repository credential for a private git
remote, which is inherent to bootstrapping GitOps.

`tests/test_namespaces.py` enforces the second one as an assertion rather than a
comment.

## Layout

| Path | Purpose |
|---|---|
| `__main__.py` | Orchestration only. Calls the modules, then exports. |
| `oke/config.py` | Loads and validates all stack config. No resources. |
| `oke/networking.py` | VCN, gateways, route table, security lists, subnets |
| `oke/cluster.py` | Availability domain and image discovery, cluster, node pool |
| `oke/identity.py` | OKE `DynamicGroup` for Workload Identity |
| `oke/kubeconfig.py` | Fetches and writes `out/oke_kubeconfig` |
| `oke/argocd.py` | Argo CD Helm release and bootstrap Application |
| `oke/namespaces.py` | The two namespaces GitOps writes into |
| `oke/wireguard.py` | Optional tunnel script for OKE nodes |
| `gitops/bootstrap/` | Manifests synced by the bootstrap Application |

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
- **`serviceAccountRef.namespace` is required** on a `ClusterSecretStore`. Without
  it Workload Identity has no ServiceAccount to resolve.
- **The OKE `DynamicGroup` lives in the tenancy root**, not in the configured
  compartment. IAM resources are tenancy-scoped.

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

The Workload Identity dynamic group and the VCN are both removed. Vault secrets
are untouched, since Pulumi never owned them.
