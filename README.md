# OKE on Oracle Cloud Infrastructure

Provisions an OCI managed Kubernetes (OKE) cluster with two ARM nodes, then hands
over to Argo CD so the cluster configures itself. There is no self-managed
Kubernetes control plane: OKE runs it.

This repository is a replacement for `pulumi-oci-rke`, which ran RKE2 directly on
two VMs.

## Cost and the Always Free allowance

**Read this before the first `pulumi up`.**

Oracle halved the Always Free Ampere A1 allowance on **2026-06-15**, and began
enforcing it on **2026-08-18**.

| | Before | Now (Always Free tenancy) |
|---|---|---|
| A1 OCPU-hours per month | 3,000 | **1,500** |
| A1 GB-hours per month | 18,000 | **9,000** |
| Equivalent always-on | 4 OCPU / 24 GB | **2 OCPU / 12 GB** |

The defaults here are **2 nodes x 2 OCPU / 12 GB = 4 OCPU / 24 GB total**, which
is twice the current Always Free entitlement. That matches what you already run,
so it requires a **Pay-As-You-Go** tenancy. On a Free Tier tenancy Oracle will
terminate the over-quota instances.

`pulumi preview` prints a banner when the pool exceeds the allowance. To stay
inside Always Free:

```bash
pulumi config set node-ocpus 1
pulumi config set node-memory-gbs 6
```

Set an OCI budget alert as well. The control plane is free; the node pool is the
entire bill.

Other allowances this design respects:

| Resource | Used | Always Free limit |
|---|---|---|
| Block volume | 2 x 50 GB boot + 50 GB cache + 50 GB database | 200 GB |
| Flexible Load Balancer | 1 (Envoy Gateway) | 1 |
| VCN | 1 | 2 (Free Tier) |

**Block volume is at its ceiling.** Both data volumes are already at the 50 GB
OCI Block Volume minimum, so neither can be made smaller, and two nodes need
2 x 50 GB of boot volume. The total is exactly 200 GB, which is still inside
Always Free, but nothing else fits: expanding either volume bills immediately,
and this is why the CloudNativePG cluster has no `volumeSnapshot` backup
configured, since snapshots draw on the same allowance. `oke/config.py` warns
about this at `pulumi preview` rather than rejecting it, because reaching the
ceiling is a deliberate cost decision rather than an impossible configuration.
Creating headroom means dropping a node:

```bash
pulumi config set node-count 1
```

One node frees 50 GB, at the cost of the second node the shared audio cache
relies on for co-location.

### Idle reclamation

Oracle reclaims idle A1 instances. Over a 7-day window an instance is idle if its
95th-percentile CPU **and** memory **and** network are all below 20%. A small
cluster running mostly idle services can trip this. If it happens, the fix is
raising the load, not the size. The `idle-guard` config flag is reserved for a
keep-busy DaemonSet; it is off by default.

### Out of host capacity

`VM.Standard.A1.Flex` frequently reports out of capacity in `eu-stockholm-1`.
This is capacity, not a quota problem. Retry `pulumi up`, or wait. The node pool
spans every availability domain to reduce the chance of it.

## Requirements

- Oracle Cloud Infrastructure account with the Always Free or Pay-As-You-Go tier
- An OCI API signing key in `~/.oci/config` (see the [OCI docs](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm))
- Python 3.14 with [uv](https://docs.astral.sh/uv/)
- [Pulumi](https://www.pulumi.com/docs/get-started/install/)

## Setup

```bash
uv sync --locked
source .venv/bin/activate

pulumi login --local
pulumi stack init oke-k8s
```

Then configure the stack:

```bash
# Required
pulumi config set --secret compartment-id ocid1.tenancy.oc1..<tenancy>
pulumi config set ssh-public-key-path /path/to/id_ed25519.pub

# Cluster and network
pulumi config set vcn-cidr 10.10.0.0/16
pulumi config set endpoint-subnet-cidr 10.10.0.0/24
pulumi config set nodes-subnet-cidr 10.10.1.0/24
pulumi config set pods-cidr 10.244.0.0/16
pulumi config set services-cidr 10.96.0.0/16
pulumi config set node-count 2
pulumi config set node-ocpus 2
pulumi config set node-memory-gbs 12
pulumi config set boot-volume-size-gbs 50

# Argo CD and GitOps
pulumi config set argocd-repo-url pasmon/pulumi-oci-oke
pulumi config set argocd-repo-target-revision main
pulumi config set argocd-repo-path gitops/bootstrap
# For a private remote, exactly one of:
pulumi config set argocd-repo-username <user>
pulumi config set --secret argocd-repo-password <token>
pulumi config set argocd-managed-by-pulumi true

# TLS. The token itself lives in OCI Vault, never in this config.
pulumi config set tls-domain radio.example.com
pulumi config set cloudflare-email you@example.com

# Workload Identity. Identifiers only, never credentials.
pulumi config set tenancy-id ocid1.tenancy.oc1..<tenancy>
pulumi config set vault-id ocid1.vault.oc1.<region>.ocid1..
pulumi config set eso-service-account-name external-secrets
pulumi config set eso-service-account-namespace external-secrets
```

Deploy:

```bash
pulumi up
```

The kubeconfig lands in `out/oke_kubeconfig`:

```bash
export KUBECONFIG=out/oke_kubeconfig
kubectl get nodes
```

### Reaching the Argo CD UI

Argo CD is deliberately **not** exposed. It is a `ClusterIP` service with no
ingress, so reach it through the kubeconfig:

```bash
kubectl -n argocd port-forward svc/argocd-server 8080:80
```

Then open <http://localhost:8080>.

## Secrets

**No application secret is ever created by Pulumi.** Credentials live in OCI
Vault and reach the cluster through External Secrets Operator, authenticated by
OKE Workload Identity. Pulumi creates only the `cert-manager` and `monitoring`
namespaces so the synced Secrets have somewhere to land.

Create these in OCI Vault before enrolling Workload Identity:

| Vault secret | Keys | Consumer |
|---|---|---|
| `radio-cloudflare-dns01` | `apiToken` | cert-manager Cloudflare dns-01 solver |
| `radio-grafana-cloud` | `username`, `password` | `k8s-monitoring` destination auth |

### Enrolling Workload Identity

This is the one manual step, and it cannot be automated: a Workload Identity
policy can only be assigned by a dynamic group, and Pulumi cannot mint OKE
service-account tokens. Budget about ten minutes.

1. Create the Vault secrets above in the OCI console.
2. Create an OKE cluster API key and a `DynamicGroup` matching ESO's ServiceAccount:

   ```
   spiffe://<cluster-ocid>/ns/external-secrets/sa/external-secrets
   ```

3. `pulumi up` creates the cluster `OKE` dynamic group. Read its id:

   ```bash
   pulumi stack output oke_dynamic_group_id
   ```

4. Attach your API-key dynamic group to that dynamic group in the console. This
   is the step that cannot be scripted.
5. Fill in the placeholders in `gitops/bootstrap/cluster-external-secrets.yaml`
   and commit. Use the annotation prefix from
   `pulumi stack output eso_dynamic_group_annotation_prefix`.

After that, Argo CD syncs the `ClusterSecretStore` and the `ExternalSecret`
objects from `argo-apps`, and all three credentials appear in-cluster.

Verify:

```bash
kubectl get clustersecretstore oci-vault     # expect Ready: True
kubectl get externalsecrets -A               # expect SecretSynced
```

## Argo CD self-management

Pulumi installs Argo CD and seeds a `bootstrap-root` Application pointing at
`gitops/bootstrap/`. From there Argo CD takes over and syncs
`argo-apps` on its own. Nothing else needs running.

Argo CD cannot own itself while Pulumi also owns the release, so the handoff is
explicit:

```bash
pulumi config set argocd-managed-by-pulumi false
pulumi up                                    # destroys the Helm release
```

Then add automation to `argocd-self` in `gitops/bootstrap/argocd-self-application.yaml`:

```yaml
syncPolicy:
  automated:
    prune: true
    selfHeal: true
  syncOptions:
    - CreateNamespace=true
```

Commit. `bootstrap-root`, the namespaces and any repository Secret survive,
because none of them are release-owned resources.

## What Pulumi owns, and what Argo CD owns

Pulumi creates only what needs OCI or must exist before GitOps runs. Everything
depending on a CRD that Argo CD installs lives in `argo-apps`.

| Pulumi | `argo-apps` |
|---|---|
| VCN, gateway, route table, 2 subnets, 2 security lists | `radio` namespace |
| OKE cluster and node pool | `EnvoyProxy`, `GatewayClass`, `public-gateway` |
| OKE `DynamicGroup` for Workload Identity | `ClusterSecretStore` and `ExternalSecret`s |
| `out/oke_kubeconfig` | `ClusterIssuer`, `Certificate` |
| `argocd` namespace, Helm release, optional repository Secret | `radio-audio-cache` PVC |
| `cert-manager` and `monitoring` namespaces | `k8s-monitoring` |
| `bootstrap-root` Application | |

The reason for the split is that Pulumi cannot create anything whose CRD Argo CD
installs, which keeps a single `pulumi up` sufficient. There is no `kubectl
wait` and no second apply.

### Why the node subnet is public

The OCI cloud-controller-manager creates the Envoy Gateway load balancer in the
subnet of its backends. A public node subnet gives that load balancer a public IP
with no `oci-load-balancer-subnet-id` annotation, which is what lets the
`EnvoyProxy` stay static YAML in `argo-apps`. A private node subnet would force
the annotation, force Pulumi to own the `EnvoyProxy`, and force the two-stage
apply this design avoids.

Consequence: worker nodes get public IPs, the same as the RKE2 cluster they
replace.

## Upgrading the Kubernetes version

1. Set the new version and apply. This updates the control plane and the node
   pool image. It does not roll the nodes.

   ```bash
   pulumi config set kubernetes-version v1.32.0
   pulumi up
   ```

   Only versions OKE advertises are supported. Check with:

   ```bash
   oci ce cluster get --cluster-id "$(pulumi stack output cluster_id)" \
     | jq -r '.data."available-kubernetes-upgrades"'
   ```

2. Roll the nodes one at a time so the workloads do not all move at once:

   ```bash
   kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
   kubectl cordon <node>
   oci compute instance terminate --force --instance-id <instance-ocid>
   ```

   The node pool replaces the instance automatically. Wait for it to return to
   `Ready` before moving to the next node.

## Cutover from `pulumi-oci-rke`

The two clusters cannot both run at 4 OCPU / 24 GB against a 2 OCPU / 12 GB
Allowance, and both want a VCN. After the OKE cluster is serving traffic:

```bash
cd ../pulumi-oci-rke
pulumi destroy --stack oci-k8s --yes
```

That frees the RKE2 VCN and its share of the compute allowance. Confirm you have
the OKE kubeconfig exported and `kubectl get nodes` is green before destroying.

## Optional: Wireguard tunnel

Lets cluster workloads reach services on your home LAN without exposing them.

```bash
pulumi config set wireguard-peer-endpoint <public IP or DDNS of your router>
pulumi config set wireguard-peer-public-key <router public key>
pulumi config set --secret wireguard-private-key <shared node private key>
pulumi config set --secret wireguard-preshared-key <tunnel preshared key>
pulumi config set wireguard-subnet-cidr 10.99.0.0/24
pulumi config set wireguard-allowed-cidrs '["192.168.88.200/32"]'
pulumi up
```

The whole block is skipped unless `wireguard-peer-endpoint` is set. Setting the
endpoint without the keys fails validation rather than provisioning a broken
tunnel.

### Addressing

Both OKE nodes are workers and share one identical node user-data script, so
each derives its tunnel address from its own private IP:

```
tunnel_ip = 10.99.0.<last octet of the node's private IP>
```

The router takes `.1` and the nodes take their derived addresses, so **the
router needs no change**. It already holds a single peer entry for the cluster
with the same shared private key. Set `RADIO_API_WIREGUARD_CIDR=10.99.0.0/24` on
the Raspberry Pi.

Pod traffic to the LAN is masqueraded onto the node's tunnel address, because pod
addresses fall outside the tunnel range and would otherwise be rejected by the
Pi's network guard. The rules are `wg-quick` `PostUp`/`PostDown` hooks rather than
cloud-init, so they survive an `iptables -F` and are restored whenever the
interface comes up.

No security list rule is needed for the tunnel itself, because the nodes dial the
router outbound.

## Development

```bash
uv sync --locked
uv run pytest -v
uv run ruff check .
uv run black --check .
uv run pylint __main__.py oke tests
uv run bandit -c pyproject.toml -r . -x ./tests -x ./.venv
pre-commit install
```

Tests run against mocked OCI and Kubernetes providers, so no credentials are
needed and nothing is created.

## Layout

| Path | Purpose |
|---|---|
| `__main__.py` | Orchestration: config, then modules, then exports |
| `oke/config.py` | Configuration loading and validation |
| `oke/networking.py` | VCN, gateway, route table, security lists, subnets |
| `oke/cluster.py` | AD and image discovery, cluster, node pool |
| `oke/identity.py` | OKE `DynamicGroup` for Workload Identity |
| `oke/kubeconfig.py` | Fetches and writes `out/oke_kubeconfig` |
| `oke/argocd.py` | Argo CD release and bootstrap Application |
| `oke/namespaces.py` | The two namespaces GitOps writes into |
| `oke/wireguard.py` | Optional tunnel script |
| `gitops/bootstrap/` | Synced by the bootstrap Application |

## Reference

- [Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [OKE documentation](https://docs.oracle.com/en-us/iaas/Content/ContEng/Concepts/contengaboutk8sversions.htm)
- [External Secrets Oracle provider](https://external-secrets.io/latest/provider/oracle-vault/)
