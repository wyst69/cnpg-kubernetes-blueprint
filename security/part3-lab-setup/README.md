# CNPG cross-cluster lab (k3d + Vault + cert-manager + ESO)

Idempotent Ansible automation of the `part3.md` walkthrough. Running the deploy
builds, **on a single Docker host**:

- **2 k3d clusters** (`cluster-a`, `cluster-b`)
- an **external HashiCorp Vault** container (dev mode, host network, `:8200`)
- **cert-manager**, **external-secrets (ESO)**, **CloudNativePG**, and **MetalLB** on both clusters
- cross-cluster **mTLS via Vault PKI** (cert-manager ClusterIssuers) and **credentials via ESO** (ClusterSecretStore)

It is a **lab**. See the security caveats below before pointing it at anything you care about.

## Files

| File | Purpose |
|------|---------|
| `deploy-lab.yml`   | Build the lab (idempotent — safe to re-run) |
| `teardown-lab.yml` | Delete the clusters + Vault container |
| `inventory.ini`    | Only needed to target a **remote** Docker host |

## Prerequisites (NOT installed by the playbook)

- **On the Docker host:** `docker`, `kubectl`, `k3d`, `helm`, and the CloudNativePG kubectl plugin.
- **On the machine running Ansible:** `ansible-core` only — no extra collections (builtin modules only).
- **Internet access** from the host (pulls images + manifests for cert-manager, CNPG, MetalLB, ESO, Vault).
- **`sudo` on the host** — the iptables tasks need root (`become` is already configured).

## Usage

Local Docker host:

```bash
ansible-playbook deploy-lab.yml -e host_ip=<HOST_IP>
```

Remote Docker host (edit `inventory.ini` first):

```bash
ansible-playbook -i inventory.ini deploy-lab.yml -e target=<host_or_group> -e host_ip=<HOST_IP>
```

### The one knob that matters: `host_ip`

`host_ip` is the **LAN IP where in-cluster pods reach Vault** (`:8200`). It
auto-detects from the host's default route, but **set it explicitly on a
multi-homed host**. It must be a real host IP reachable from the k3d container
network — `127.0.0.1` will **not** work (pods can't reach the host loopback).

## What it changes on the host

- **iptables:** sets the `FORWARD` chain policy to `ACCEPT` and opens TCP `8200`.
  ⚠️ Teardown does **not** revert the `FORWARD` policy (its prior value is
  unknown); it only removes the port-8200 rule.
- **API server ports** are pinned to `6443` (cluster-a) and `6444` (cluster-b).
  If either is already in use, change `api_port` in the `clusters:` var.
  (Pinning keeps the API endpoint — and therefore the kubeconfig and Vault's
  `kubernetes_host` — stable across serverlb restarts/reboots.)
- **MetalLB pool** is derived from each cluster's actual Docker subnet
  (`.250.10–.100`), so it adapts to your environment.

## Teardown

```bash
ansible-playbook -i inventory.ini teardown-lab.yml -e target=<host> -e confirm=yes
```

Deletes both clusters and the Vault container. Prompts for confirmation unless
you pass `-e confirm=yes`. Leaves the `FORWARD` iptables policy as-is.

## Security caveats — this is a lab

- **Vault runs in dev mode:** HTTP (no TLS), hardcoded root token `root`,
  **in-memory storage**. If the Vault container restarts, **all its config is
  wiped** (auth paths, PKI root CA, policies) — cert-manager/ESO break until you
  re-run deploy, and a regenerated root CA invalidates previously issued certs.
  Never use this Vault setup for anything real.
- **Floating tags:** the Vault image (`:latest`) and the ESO CRD bundle (from
  `main`) are not version-pinned, so results can drift over time. cert-manager
  (v1.21.1), CloudNativePG (1.30), and MetalLB (v0.16.1) **are** pinned.
- It provisions real infrastructure and opens a firewall port — don't aim it at
  a shared or production host.

## Known failure mode

If the host **crashes or loses power** while the clusters are up, k3d's on-disk
state can corrupt — pods come back `CrashLoopBackOff` with `exec format error`.
It is not repairable in place: **run `teardown-lab.yml`, then redeploy.**

## Verifying a successful deploy

On the Docker host, both clusters should show the store as `Valid`/`Ready` and
both issuers as `Ready`:

```bash
kubectl get clustersecretstore,clusterissuer --context k3d-cluster-a
kubectl get clustersecretstore,clusterissuer --context k3d-cluster-b
```
