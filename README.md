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
pulumi config set service-subnet-cidr 10.10.2.0/24
pulumi config set pods-cidr 10.244.0.0/16
pulumi config set services-cidr 10.96.0.0/16
pulumi config set node-count 2
pulumi config set node-ocpus 2
pulumi config set node-memory-gbs 12
pulumi config set boot-volume-size-gbs 50

# The GitOps repository Argo CD is handed over to. Pulumi seeds exactly one
# Application at this path; everything else comes from there.
pulumi config set argocd-repo-url https://github.com/pasmon/argo-apps.git
pulumi config set argocd-repo-target-revision main
pulumi config set argocd-repo-path app-of-apps

# For a private remote, exactly one of:
pulumi config set argocd-repo-username <user>
pulumi config set --secret argocd-repo-password <token>
# or
cat ~/.ssh/id_ed25519 | pulumi config set --secret argocd-repo-ssh-private-key
# or a GitHub App, as a token with no expiry to revoke:
pulumi config set argocd-github-app-id <app id>
pulumi config set argocd-github-app-installation-id <installation id>
cat my-app.private-key.pem | pulumi config set --secret argocd-github-app-private-key
# Private keys and PEMs are multi-line, so they have to be piped in on stdin.
# The `@<path>` form that earlier revisions of this file suggested is not a
# Pulumi feature: `pulumi config set x @key.pem` stores the literal string
# "@key.pem" and exits 0, so the breakage is silent. Passing the key as an
# argument does not work either, because a PEM's second line starts with
# "-----" and the CLI reads it as a flag. Only the pipe works.

# Private keys and PEMs are multi-line, so they have to be piped in on stdin.
# The `@<path>` form that earlier revisions of this file suggested is not a
# Pulumi feature: `pulumi config set x @key.pem` stores the literal string
# "@key.pem" and exits 0, so the breakage is silent. Passing the key as an
# argument does not work either, because a PEM's second line starts with
# "-----" and the CLI reads it as a flag. Only the pipe works.

# OCI Vault access for External Secrets Operator. Both are required together:
# the policy is created in the tenancy root and scoped to the vault.
pulumi config set tenancy-id ocid1.tenancy.oc1..<tenancy>
pulumi config set vault-id ocid1.vault.oc1.eu-stockholm-1.<vault>
```

Use the full `https://…git` URL rather than a `owner/repo` shorthand. Pulumi
writes that string verbatim into the repository Secret's `url` field, and Argo CD
matches credentials against the exact URL its Application requests, so a
shorthand produces a Secret that does not apply to the Application it was meant
to authenticate.

The GitHub App needs `Contents: read` on the repository and must be installed on
it. All three `argocd-github-app-*` values have to be set together; setting one
or two of them fails at `pulumi preview` rather than half-configuring Argo CD.
Once the Argo CD release is handed over to `argo-apps`, the repository Secret
Pulumi created is left alone — `argo-apps` does not redeclare it, so it keeps
authenticating the seed Application.

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
Vault and reach the cluster through External Secrets Operator, authenticated as
an instance principal. Pulumi creates only the `cert-manager`, `monitoring` and
`external-secrets` namespaces so the synced Secrets have somewhere to land.

Create these in OCI Vault before configuring the policy. `argo-apps` documents
the full list; these two are needed for the certificate and monitoring to come
up:

| Vault secret | Keys | Consumer |
|---|---|---|
| `radio-cloudflare-dns01` | `apiToken` | cert-manager Cloudflare dns-01 solver |
| `radio-grafana-cloud` | `username`, `password` | `k8s-monitoring` destination auth |

Each holds a JSON object, so a `remoteRef` that omits `property` receives the
whole document rather than the credential.

### Granting vault access

There is no manual step. `pulumi up` creates the policy, and it is the whole of
Pulumi's IAM footprint:

1. Create the Vault secrets above in the OCI console.
2. Set `tenancy-id` and `vault-id` to the OCIDs from that Vault.
3. `pulumi up`.

The policy grants `read` on the `secret-family` of that one vault to OCI's
built-in `oke` dynamic group, which matches every node instance in the tenancy:

```
Allow group oke to read secret-family in compartment <compartment> where all {
request.principal.type = 'instance',
target.vault.id = '<vault-ocid>'}
```

When the vault sits in the tenancy root, so `compartment-id` is the tenancy
OCID, the clause reads `in tenancy` instead. OCI rejects a tenancy OCID on the
left of a statement with `Compartment {...} does not exist or is not part of
the policy compartment subtree`; the root is spelled `tenancy`.

Every tenancy already has that dynamic group, which is why Pulumi creates no
`DynamicGroup` and why there is no console step at all.

Verify:

```bash
kubectl get clustersecretstore oci-vault     # expect Ready: True
kubectl get externalsecrets -A               # expect SecretSynced
```

### Why instance principals and not Workload Identity

OKE Workload Identity is finer grained: it identifies a pod by cluster,
namespace and service account, so IAM can name one workload. It is also only
available on **enhanced** clusters, which OCI bills at $0.10 per cluster-hour
(~$74/month cap). This program creates a *basic* cluster on purpose, because that
control plane is free and Always Free is the point.

An instance principal identifies the compute instance instead, so the grant
reaches any pod running on a node rather than just ESO. That is a real widening
of scope. It is accepted because the policy is restricted to `read` on a single
vault, and because there is no credential-free alternative on a free cluster.
The other options, both worse: an OCI API key would put a long-lived private key
in a cluster Secret and break the rule that Pulumi creates no application secret,
and moving secrets into git under SOPS would drop OCI Vault entirely, rewriting
every `ExternalSecret` in `argo-apps`.

If the $74/month ever becomes acceptable, the change is `principalType: Workload`
on the `ClusterSecretStore` plus an enhanced cluster; the policy in
`oke/identity.py` would then be replaced by the workload-principal form.

## Argo CD self-management

Pulumi's entire involvement with Argo CD is four resources: the `argocd`
namespace, the Helm release, an optional repository credential, and one
`Application` named `bootstrap-root`. That Application points at
`argocd-repo-path` in the GitOps repository with `directory: recurse`, so the
repository declares its own entrypoint rather than this program hardcoding a
file. Everything after that, **including Argo CD itself**, is `argo-apps`.

Two consequences worth knowing:

- The bootstrap Application uses the `default` project. Argo CD creates that
  project itself, permissive, so a hand-over needs no AppProject to exist first.
  A named project would have to be created by the very Application that
  references it.
- There is no `gitops/` directory in this repository any more. If one reappears,
  something has reintroduced an intermediate step between the release and
  `argo-apps`, and `tests/test_program_wiring.py` will fail.

Argo CD cannot own itself while Pulumi also owns the release, so the hand-off is
explicit:

```bash
pulumi config set argocd-managed-by-pulumi false
pulumi up                        # stops tracking the release; deletes nothing
```

The release is created with `retain_on_delete`, so this `pulumi up` makes Pulumi
forget the release rather than deleting it. Nothing in the cluster changes and
there is no interruption. `argo-apps/core-apps/argo-cd.yaml` then owns the
already-running release, configured with
`automated: {prune: true, selfHeal: true}`. It pins the same chart version this
program installs; the two must be changed together or adopting the release
downgrades Argo CD under the running cluster.

Do not remove `retain_on_delete` to "tidy up". Deleting the resource instead
means `helm uninstall` against a live cluster, which takes Argo CD's ConfigMaps,
Secrets and ServiceAccounts with it. Argo CD cannot recover from that on its own:
the application controller reads `argocd-cm` while starting up and exits fatally
when it is missing, and it has lost the ServiceAccount it would need to recreate
anything. Every Application then sits at `Unknown` until the objects are
restored by hand. `pulumi destroy` is unaffected and still removes everything.

`bootstrap-root`, the namespaces and any repository Secret survive, because none
of them are release-owned resources.

## What Pulumi owns, and what Argo CD owns

Pulumi creates only what needs OCI or must exist before GitOps runs. Everything
depending on a CRD that Argo CD installs lives in `argo-apps`.

| Pulumi | `argo-apps` |
|---|---|
| VCN, gateway, route table, 3 subnets, 3 security lists | `radio` namespace |
| OKE cluster and node pool | `EnvoyProxy`, `GatewayClass`, `public-gateway` |
| IAM policy granting nodes read on the vault | `ClusterSecretStore` and `ExternalSecret`s |
| | `ClusterIssuer`, `Certificate` |
| `out/oke_kubeconfig` | `radio-audio-cache` PVC |
| `argocd` namespace, Helm release, optional repository Secret | `k8s-monitoring` |
| `cert-manager`, `monitoring`, `external-secrets` namespaces | Argo CD's own Helm release |
| `bootstrap-root` Application | |

The reason for the split is that Pulumi cannot create anything whose CRD Argo CD
installs, which keeps a single `pulumi up` sufficient. There is no `kubectl
wait` and no second apply.

### Why there are three subnets

`endpoint` carries the Kubernetes API, `nodes` carries the workers, and
`service` carries the OKE-managed load balancers. The third one is not
optional: OKE rejects a node pool placed in a subnet registered as
`service_lb_subnet_ids`, with *"The service subnets cannot be used by node
pools"*. Sharing one subnet for both fails the node pool create. Its security
list allows public TCP/443 for the Envoy Gateway listener and the TCP backend
range OCI's load balancer uses to reach its backends.

### Why the node subnet is public

The OCI cloud-controller-manager creates the Envoy Gateway load balancer in the
subnet of its backends, not in the service subnet. A public node subnet gives
that load balancer a public IP with no `oci-load-balancer-subnet-id` annotation,
which is what lets the `EnvoyProxy` stay static YAML in `argo-apps`. A private
node subnet would force the annotation, force Pulumi to own the `EnvoyProxy`, and
force the two-stage apply this design avoids.

Consequence: worker nodes get public IPs, the same as the RKE2 cluster they
replace.

## Upgrading the Kubernetes version

`kubernetes-version` is unset by default, and an unset version means "take the
newest one OKE advertises". Leave it unset and every `pulumi up` upgrades the
cluster and replaces the node pool whenever OCI publishes a release, which is a
surprise apply on a two-node cluster. Pin it, as in step 1 below, to upgrade
deliberately.

The pin is local. `Pulumi.oke-k8s.yaml` holds the encryptionsalt and the
secrets, so it is not tracked, and a stack brought up elsewhere starts unpinned
again.

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

Setting node user data replaces the cloud-init OKE would otherwise supply, and
that cloud-init is what joins a node to the cluster. The tunnel script therefore
runs OKE's own bootstrap (`oke_init_script` from instance metadata) first and
sets up the tunnel afterwards, so a tunnel failure never keeps a node out.

### Router configuration

The two sides need different halves of the key pair, which is the easiest thing
to get wrong here because nothing in the node's output says so:

- `wireguard-peer-public-key` is the **router's** public key. The nodes put it
  in their `[Peer] PublicKey`.
- The router's peer entry needs the **nodes'** public key, which this program
  never prints because it is derived from the secret `wireguard-private-key`.

Derive the node key locally, from the same private key you set in the config:

```bash
echo <wireguard-private-key> | wg pubkey
```

On MikroTik RouterOS that is one peer, not two. Both nodes share
`wireguard-private-key`, so they share one public key:

```routeros
/interface/wireguard/peers/print detail
```

```routeros
/interface/wireguard/peers/add interface=wg-radio \
    public-key=<node public key> allowed-address=10.99.0.0/24
```

`endpoint-address` stays empty: the nodes are public and dial the router, so
the router learns each endpoint from the handshake.

**If `rx` and `tx` are both zero while the node reports bytes sent, the peer's
`public-key` is the router's own key.** A peer configured with the router's key
points at itself, and the handshake can never complete. This fails silently:
the nodes transmit, nothing is ever received, and no error is reported on either
side. Check that `public-key` on the peer is the node key and not the same
value as `/interface/wireguard/print` reports for the interface.

If UDP cannot reach the router at all, the WAN interface is not named `wan` on
most RouterOS devices:

```routeros
/ip firewall filter/add chain=input protocol=udp dst-port=51820 \
    in-interface=ether1 action=accept place-before=0
```

### Addressing

Both OKE nodes are workers and share one identical node user-data script, so
each derives its tunnel address from its own private IP:

```
tunnel_ip = 10.99.0.<last octet of the node's private IP>
```

The router needs a single peer entry for the whole cluster, using the node
public key and `wireguard-subnet-cidr` as its `allowed-address`. Its own tunnel
address does not have to be inside `wireguard-subnet-cidr`: traffic to a LAN
host is an ordinary forward on the router, so the router's tunnel subnet only
has to be distinct from the nodes'. Keep it distinct anyway, or the two ends
disagree about which subnet is the tunnel.

`RADIO_API_WIREGUARD_CIDR` on the Raspberry Pi must be the **nodes'** tunnel
subnet, `10.99.0.0/24`, because that is the source address pod traffic is
masqueraded onto.

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
| `oke/identity.py` | The IAM policy granting nodes read on the vault |
| `oke/kubeconfig.py` | Fetches and writes `out/oke_kubeconfig` |
| `oke/argocd.py` | Argo CD release and the single bootstrap Application |
| `oke/namespaces.py` | The three namespaces GitOps writes into |
| `oke/wireguard.py` | Optional tunnel script |

There is no `gitops/` directory. This repository ships manifests to Kubernetes
nowhere; `argo-apps` is the only GitOps tree, which is what makes the hand-over
a single Application rather than a chain.

## Reference

- [Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [OKE documentation](https://docs.oracle.com/en-us/iaas/Content/ContEng/Concepts/contengaboutk8sversions.htm)
- [External Secrets Oracle provider](https://external-secrets.io/latest/provider/oracle-vault/)
