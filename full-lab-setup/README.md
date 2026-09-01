# CNPG cross-cluster lab — management hub + workload clusters

Idempotent Ansible automation that builds, **on a single Docker host**:

| | |
|---|---|
| **`hub`** | management hub k3d cluster — **OpenBao** or **HashiCorp Vault** (PKI + kv-v2) and **MinIO** (S3), plus the monitoring stack when it is enabled |
| **`cluster-a`, `cluster-b`** | symmetrical workload clusters — cert-manager, external-secrets (ESO), CloudNativePG, MetalLB, barman-cloud plugin |
| | cross-cluster **mTLS via the PKI engine** and **credentials via ESO**, both anchored on the hub |

All three clusters share one Docker bridge network, which is what lets a MetalLB
address on the hub answer for pods in the workload clusters.

It is a **lab**. See the caveats near the bottom before pointing it at anything
you care about.

```
                          ┌──────────────────────── hub (k3d) ────────────────────────┐
                          │  OpenBao 172.20.50.10:8200  MinIO  172.20.50.11:9000      │
                          │  Prometheus .13:9090        Grafana .12:80                │
                          └───────────────▲────────────────────▲──────────────────────┘
                                          │ ESO + cert-manager │ barman-cloud
                          ┌───────────────┴──────┐   ┌─────────┴────────────┐
                          │ cluster-a (k3d)      │   │ cluster-b (k3d)      │
                          │ CNPG · ESO · certmgr │   │ CNPG · ESO · certmgr │
                          └──────────────────────┘   └──────────────────────┘
                            all on Docker network multi-cluster-net (172.20.0.0/16)
```

## Layout

```
site.yml                      deploy everything
teardown.yml                  delete everything
inventory.ini                 only needed for a REMOTE Docker host
ansible.cfg
group_vars/all/
  main.yml                    topology: network, clusters, endpoints
  components.yml              which optional tools get installed
  versions.yml                pinned manifest URLs and chart versions
roles/
  host_prep                   Docker network, iptables, sysctls, mc, ~/.bashrc
  k3d_clusters                create hub + workload clusters, merge kubeconfig
  storage                     VolumeSnapshot CRDs + CSI hostpath + StorageClasses
  metallb                     one L2 address pool per cluster
  hub_vault                   OpenBao/Vault on the hub: deploy, init/unseal, engines, PKI, policies
  hub_minio                   MinIO on the hub + buckets
  cert_manager                cert-manager + the RBAC the TokenReview call needs
  external_secrets            ESO CRDs + operator
  cnpg                        CloudNativePG operator
  barman_plugin               barman-cloud CNPG-I plugin            (optional)
  vault_k8s_auth              one Kubernetes auth mount per workload cluster
  vault_integration           ClusterSecretStore + the two ClusterIssuers
  monitoring                  Prometheus/Grafana or SigNoz          (optional)
```

## Prerequisites (NOT installed by the playbook)

- **On the Docker host:** `docker`, `kubectl`, `k3d`, `helm`, and the
  CloudNativePG kubectl plugin.
- **On the Ansible controller:** `ansible-core` plus the `community.docker`
  collection (the shared network and the legacy-Compose teardown) and
  `ansible.posix` (firewalld, on RHEL-family hosts only — but the collection is
  needed either way for the play to parse).
- **Internet access** from the host — it pulls images and manifests for k3s,
  OpenBao, MinIO, cert-manager, ESO, CNPG, MetalLB and the CSI hostpath driver.
- **`sudo` on the host** — the iptables, sysctl and apt tasks escalate.

### Sizing

The base lab consumes 5 GB of RAM just after deployment.
The monitoring, whichever flavor you chose, adds 3 GB.

#### Deploy time

| Configuration | Wall clock |
|---|---|
| base lab (`monitoring.enabled: false`) | **around 5min** |
| + `kube-prometheus` | **around 8min** |
| + `otel-signoz` | **around 8min** |

#### Flavor comparison

`otel-signoz` is marginally *lighter* overall — 7.6 vs 8.0 GiB anon — but the
distribution differs, and the totals are close enough to treat as equivalent:

Pick on capability, not footprint. They do not collect the same things:
kube-prometheus has kube-state-metrics (Kubernetes object state) and PromQL;
otel-signoz has container logs and traces. `otel-signoz` also takes about a
minute longer to deploy, and its hub figure is a freshly-deployed floor — under
sustained ingest ClickHouse will grow past it, which is less true of Prometheus
since the agents' remote-write sizes its head block from the start.

#### Disk

| PVC | Request | Set in |
|---|---|---|
| MinIO data | 20 Gi | `minio_data_size` |
| Prometheus | 10 Gi | `prometheus_storage_size` |
| Secrets manager data | 2 Gi | `secrets_data_size` |
| ZooKeeper (otel-signoz) | 8 Gi | signoz chart |
| SigNoz db (otel-signoz) | 1 Gi | signoz chart |

Those add up to more than the disk actually consumed. All provisioners the
lab can use simply create a **directory**.
Nothing is preallocated, so a 20 Gi request costs nothing until it is filled.

The flip side is that **neither provisioner enforces the request either**. A
runaway WAL archive in MinIO will keep growing past 20 Gi until the host's
filesystem is full, and you will hit `no space left on device` rather than a
clean `PVC full`.

Why three provisioners are available? Because they all have limitations.
- csi-nfs (default) is an in-cluster NFS server + nfs.csi.k8s.io: volume
expansion and VolumeSnapshots, and — unlike the csi-hostpath — PVs aren't
pinned to one node, so pod anti-affinity actually works. NFS is a known
anti-pattern for PGDATA in production; accepted here as a lab-only trade.
- csi-hostpath gives you volume expansion and VolumeSnapshots, but every PV
is pinned to the single node the driver runs on — anti-affinity
won't spread pods across nodes.
- local-path is k3s's built-in provisioner: lighter and quicker to bring up,
but no expansion, no snapshots.


**VolumeSnapshots are the sharpest edge here.** The CSI hostpath and CSI NFS drivers are not a
real storage backend: it has no copy-on-write and no incrementals. Each snapshot
is a **complete gzip archive of the whole volume**, taken with `tar czf` — from
the driver's own source:
So *n* snapshots of a Postgres volume cost roughly *n* × (compressed volume
size), on the same filesystem as the volume itself, with no dedup between them.

## Usage

```bash
# local Docker host
ansible-playbook site.yml -e host_ip=<HOST_IP>

# remote Docker host (edit inventory.ini first)
ansible-playbook site.yml -e target=docker_host -e host_ip=<HOST_IP>
```

Re-running is safe and expected — every step is guarded.

### The one knob that always matters: `host_ip`

`host_ip` auto-detects from the host's default route. Set it explicitly on a
multi-homed host. It has to be a **real host IP**, not `127.0.0.1`: it is baked
into each cluster's API server certificate as a SAN at creation time, so
changing it later means recreating the clusters.

### Selecting parts of the run

Every role carries tags:

```bash
ansible-playbook site.yml --tags monitoring        # just the monitoring stack
ansible-playbook site.yml --tags hub               # just the secrets manager + MinIO
ansible-playbook site.yml --tags vault             # the secrets manager + its per-cluster auth wiring
ansible-playbook site.yml --skip-tags storage
```

Tags select *what runs now*. The component switches below decide *what the lab
consists of* — they are the ones teardown and re-runs respect.

## Optional components

`group_vars/all/components.yml` holds two dicts:

- **`lab_component_defaults`** — the catalogue. Every optional tool the lab
  knows how to install, with its shipped defaults. Read-only; it documents what
  exists.
- **`lab_components`** — your override layer. A *partial* dict, merged
  recursively over the catalogue into the `components` fact that roles read. You
  only state the keys you want to change.

```bash
# no monitoring at all
ansible-playbook site.yml -e '{"lab_components":{"monitoring":{"enabled":false}}}'

# CRDs only, so PodMonitors apply but nothing scrapes them yet
ansible-playbook site.yml -e '{"lab_components":{"monitoring":{"stack":"crds_only"}}}'

# lighter storage: k3s local-path, no expansion, no snapshots
ansible-playbook site.yml -e '{"lab_components":{"storage":{"driver":"local-path"}}}'

# HashiCorp Vault instead of the OpenBao default
ansible-playbook site.yml -e '{"lab_components":{"secrets":{"provider":"vault"}}}'
```

An unknown component name, an invalid `monitoring.stack` or an invalid
`secrets.provider` fails in `pre_tasks`, before anything is touched.

Shipped catalogue:

| Component | Default | Settings |
|---|---|---|
| `secrets` | — | `provider: openbao\|vault` |
| `storage` | on | `driver: csi-hostpath\|local-path`, `snapshots: true\|false` |
| `barman_plugin` | on | — |
| `monitoring` | on | `stack: crds_only\|kube-prometheus\|otel-signoz`, `workload_agents`, `retention` |

The mandatory pieces — the hub cluster, the secrets manager, MinIO, cert-manager,
ESO, CNPG, MetalLB — deliberately have **no switch**. They are what the lab is.
`secrets` has no `enabled` key for that reason: which server runs is a choice,
whether one runs is not.

## Monitoring variants

| `stack` | Hub | Workload clusters |
|---|---|---|
| `crds_only` | — | Prometheus Operator CRDs only |
| `kube-prometheus` | kube-prometheus-stack: Prometheus (remote-write receiver), Grafana, Alertmanager | operator in **PrometheusAgent** mode, remote-writing to the hub (`workload_agents: true`) |
| `otel-signoz` | SigNoz | OpenTelemetry collector DaemonSets shipping OTLP to the hub |

Cross-cluster *scraping* is deliberately not attempted — it would mean exposing
every kubelet. The agents push instead, so CNPG's PodMonitors stay cluster-local
while all the data lands centrally, labelled with `cluster=<name>`.

## Endpoints

From the Docker host (k3d publishes the hub's nodePorts on localhost):

| Service | Host | From inside the clusters |
|---|---|---|
| OpenBao / Vault API+UI | http://127.0.0.1:8200 | http://172.20.50.10:8200 |
| MinIO S3 | http://127.0.0.1:9000 | http://172.20.50.11:9000 |
| MinIO console | http://127.0.0.1:9001 | — |
| Grafana | http://127.0.0.1:3000 | http://172.20.50.12 |
| Prometheus | http://127.0.0.1:9090 | http://172.20.50.13:9090 |
| SigNoz UI | http://127.0.0.1:8080 | http://172.20.50.14:8080 |

The SigNoz OTLP endpoint is not in that table because nothing on the host needs
it: workload collectors reach it in-cluster at `172.20.50.15:4317`. It has its
own address because MetalLB will not give two Services the same IP.

Credentials: the root token and unseal key land in `~/.openbao-keys.json`, or
`~/.vault-keys.json` under the `vault` provider (mode 0600). The managed
`~/.bashrc` block exports the matching pair — `BAO_ADDR`/`BAO_TOKEN` or
`VAULT_ADDR`/`VAULT_TOKEN`. MinIO is `minioadmin`/`minioadmin`. Grafana is
`admin`/`prom-operator`. SigNoz is `admin@lab.local`/`Signoz@Lab123` (org `lab`),
created by the playbook — see [The otel-signoz variant](#the-otel-signoz-variant).

Open a new shell (or `source ~/.bashrc`) after a deploy to pick up those and the
`kch` / `kca` / `kcb` context-switch aliases.

### Pointing your `ObjectStore` manifests at the hub

```yaml
    endpointURL: http://172.20.50.11:9000
    destinationPath: s3://cnpg-backups/
```

The `cnpg-backups` bucket is created for you.

## What it changes on the host

- **iptables:** sets the `FORWARD` chain policy to `ACCEPT` and opens the six
  host ports the hub publishes. Teardown removes the port rules; it does **not**
  revert the `FORWARD` policy, whose prior value is unknown.
- **sysctls:** raises `fs.inotify.max_user_watches` / `max_user_instances`.
  Three k3d clusters exhaust the stock budget. Not reverted by teardown.
- **API server ports** are pinned — `6443` cluster-a, `6444` cluster-b, `6445`
  hub. If one is taken, change `api_port` in `group_vars/all/main.yml`. Pinning
  keeps the kubeconfig and the `kubernetes_host` config stable across serverlb
  restarts and reboots.
- **`~/.bashrc`**, **`~/.kube/config`**, **`~/.vault-keys.json`**, and
  `/usr/local/bin/mc`.

## How the hub is reached — and why

Two directions, two mechanisms.

**Clusters → hub** goes over MetalLB. Every k3d node container sits on
`multi-cluster-net`, so an address the hub advertises by ARP is reachable from
pods in the other clusters. Hence the fixed `172.20.50.x` addresses: they are in
the shared subnet but far above what Docker's IPAM hands out, so they never
collide. Each hub Service is `type: LoadBalancer` **and** pins a nodePort, which
is how the same Service ends up on your localhost too.

**Hub → clusters** is the one hop the other way: the secrets manager, in a pod
on the hub, has
to call each workload cluster's API server for `TokenReview`. It does that at
`https://host.k3d.internal:<api_port>` — the port k3d publishes for that
cluster's API. `host.k3d.internal` resolves inside pods (k3d injects it) and is
added to each API server certificate by the `--tls-san` flags in the
`k3d_clusters` role. No Docker DNS from inside a pod, and container IPs can
churn freely.

## Stopping and restarting the host

Stop the clusters before rebooting, and start them again afterwards:

```bash
k3d cluster stop --all      # before `reboot`
k3d cluster start --all     # after
```

Do this even for a quick reboot. Each k3d node is a container running its own
containerd, and a graceful stop is what lets it flush state. Killed mid-write —
an abrupt reboot, a power cut, `docker kill` — it can end up unable to start at
all, which strands the node for good.

The secrets manager comes back **sealed** either way: the file backend on its PVC
survives, the unseal does not. Unseal it from the stored key with:

```bash
ansible-playbook site.yml --tags vault -e host_ip=<HOST_IP>
```

An unplanned restart is not automatically fatal, and the clusters often come
back. But when they don't, the fix is a teardown and redeploy rather than a
repair — which is what makes the two commands above worth the habit.

## Teardown

```bash
ansible-playbook teardown.yml                                 # prompts
ansible-playbook teardown.yml -e target=docker_host -e confirm=yes
```

Deleting the hub cluster deletes the secrets manager and MinIO **and their data** with it.

## Security caveats — this is a lab

- **The secrets manager is HTTP, single unseal share, root token on disk.**
- **Losing the keys file loses the lab's secrets manager** — there is no second
  share and no recovery key. `~/.openbao-keys.json`, or `~/.vault-keys.json`
  under the `vault` provider.
- **MinIO ships with the default `minioadmin` credentials**, reachable on the
  LAN over plain HTTP.
- **Floating tags:** the Prometheus Operator CRDs (from `main`) and the CSI
  hostpath driver (from `master`) are not pinned, so results drift over time.
  cert-manager, CNPG, MetalLB, the barman plugin and MinIO **are** pinned in
  `versions.yml`, and both secrets-manager images are pinned in
  `secrets_providers`. So is ESO — its CRD bundle and its Helm chart both come from
  `eso_version`, because the chart version and appVersion track 1:1 and CRDs from
  `main` can otherwise run ahead of the released controller. The remaining Helm
  chart versions there are empty by default — fill them in once you have a
  combination you like.


## Verifying a deploy

```bash
kubectl get clustersecretstore,clusterissuer --context k3d-cluster-a
kubectl get clustersecretstore,clusterissuer --context k3d-cluster-b
```

Both clusters should show the store `Valid`/`Ready` and both issuers `Ready`.
On the hub:

```bash
kubectl get pods,svc -n openbao --context k3d-hub   # -n vault under the vault provider
kubectl get pods,svc -n minio   --context k3d-hub
mc ls minio/
```

Monitoring, if enabled. `kube-prometheus` — infrastructure metrics from both
workload clusters should be present, tagged by cluster:

```bash
curl -s --data-urlencode 'query=count by (cluster) ({__name__=~"node_.+"})' \
  http://127.0.0.1:9090/api/v1/query
```

`otel-signoz` — rows in ClickHouse. Traces stay at 0 until you generate some:

```bash
kubectl exec -n signoz --context k3d-hub chi-signoz-clickhouse-cluster-0-0-0 \
  -c clickhouse -- clickhouse-client -q \
  "SELECT 'metrics', count() FROM signoz_metrics.samples_v4
   UNION ALL SELECT 'logs', count() FROM signoz_logs.logs_v2
   UNION ALL SELECT 'traces', count() FROM signoz_traces.signoz_index_v3"
```
