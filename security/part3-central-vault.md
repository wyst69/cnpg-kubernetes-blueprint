# Part 3: Centralized Vault, Multi-Cluster Topologies, and Cross-Cluster mTLS

## Preamble
To write this article I needed a specific lab environment with two K8s clusters and an external HashiCorp Vault.
I created this environment using docker and k3d.
In the folder `part3-lab-setup`, you'll find:
- a complete walkthrough for setting the same environment manually (part3-setup-lab.md)
- playbooks to automate the creation and destruction of this environment (look at README.md)
Using one method or the other will lead to the exact same result: two K8s clusters fully ready for secret management. That includes everything explained in SECTION 1, SECTION 2 and SECTION 3 Step 1 of this article.

## The Production Architecture Overview

In Part 1 and Part 2, we established the core concepts of CloudNativePG (CNPG) and built the "Contract of Trust" inside a single Kubernetes cluster using HashiCorp Vault, External Secrets Operator (ESO), and cert-manager.

But production environments rarely stay confined to a single cluster. Whether for high availability, compliance, or disaster recovery (DR), production database architectures demand multi-cluster topologies—such as a Primary database in Data Center / Region A and a Standby database in Data Center / Region B.

Running multi-cluster PostgreSQL workloads introduces two major security challenges:

- Secret & Identity Isolation: How does an external Vault validate Kubernetes ServiceAccount tokens originating from two completely distinct Kubernetes control planes without leaking permissions?

- Cross-Cluster mTLS Trust: How do PostgreSQL nodes in Cluster B establish encrypted, mutual-TLS (mTLS) streaming replication with Cluster A when each cluster runs its own internal network and cert-manager instance?

In this guide, we will build a production-grade multi-cluster CNPG setup backed by a centralized HashiCorp Vault PKI engine and External Secrets Operator.

## SECTION 1: Vault Multi-Cluster Auth Isolation (The Auth Path Pattern)

When moving from a single Kubernetes cluster to a multi-cluster architecture, a common temptation is to reuse a single Kubernetes Auth mount path in Vault.

In practice, this is a dangerous pitfall. ServiceAccounts in Kubernetes are scoped to namespaces and names—meaning cert-manager in Cluster A has the exact same name and namespace (cert-manager/cert-manager) as cert-manager in Cluster B. If both clusters share a single Auth path (e.g., auth/kubernetes), Vault has no native way to distinguish which cluster API server to contact to validate an incoming ServiceAccount JWT.

### 1. The "One Auth Mount per Cluster" Pattern

To enforce clean security boundaries and prevent cross-cluster token spoofing, Vault should mount a dedicated Kubernetes Auth engine for every cluster in your infrastructure:

- auth/k8s-cluster-a → Dedicated engine for Cluster A. Validates tokens against Cluster A's API server.

- auth/k8s-cluster-b → Dedicated engine for Cluster B. Validates tokens against Cluster B's API server.

```text
                   ┌──────────────────────────────────────────────┐
                   │        External Vault Server                 │
                   │                                              │
                   │  ┌──────────────────┐ ┌─────────────┐        │
                   │  │auth/k8s-cluster-a│ │eso-policy   │        │
                   │  └────────┬─────────┘ └──────┬──────┘        │
                   │  ┌────────┴─────────┐        │               │
                   │  │auth/k8s-cluster-b│ ┌──────┴────────────┐  │
                   │  └────────┬─────────┘ │cert-manager-policy│  │
                   │           │           └───────────────────┘  │
                   └───────────┼──────────────────────────────────┘
                               │
            ┌──────────────────┴──────────────────┐
            ▼                                     ▼
┌──────────────────────┐               ┌──────────────────────┐
│  k3d-cluster-a       │               │  k3d-cluster-b       │
│  (192.168.1.99:33011)│               │  (192.168.1.99:43037)│
└──────────────────────┘               └──────────────────────┘
```

### 2. Solving External Token Validation (TokenReview & Issuer Mismatches)

Because Vault runs outside the Kubernetes clusters, two critical authentication details must be handled during engine configuration:
- Dedicated Token Reviewer (token_reviewer_jwt): When a controller like cert-manager or external-secrets presents its token to Vault, Vault contacts the cluster's Kubernetes API server endpoint (/apis/authentication.k8s.io/v1/tokenreviews) to verify the token's validity. Since Vault is external and does not have local in-cluster credentials, we must provide a long-lived ServiceAccount JWT (token_reviewer_jwt) for each cluster.
- Disabling Local CA and Issuer Validation: Setting disable_local_ca_jwt=true forces Vault to rely on the provided token_reviewer_jwt rather than attempting local filesystem lookups. Additionally, setting disable_iss_validation=true accommodates custom token issuers used by lightweight distributions (such as K3s/k3d).

#### Vault Auth Configuration Pattern for Cluster A
```bash
vault write auth/k8s-cluster-a/config \
kubernetes_host="https://127.0.0.1:33011" \
kubernetes_ca_cert=@/tmp/ca-cluster-a.crt \
token_reviewer_jwt="$TOKEN_A" \
disable_local_ca_jwt=true \
disable_iss_validation=true
```

### 3. Handling Dynamic JWT Audiences (audience="vault")

While External Secrets Operator (ESO) uses standard ServiceAccount tokens, cert-manager uses the Kubernetes TokenRequest API to dynamically request short-lived tokens.
By default, cert-manager requests tokens with explicit audience claims. If the Vault role does not expect these claims, Vault drops the authentication request with an invalid audience (aud) claim error.
To resolve this across all clusters:
- In Vault: Define a explicit audience claim (audience="vault") on the cert-manager role.
- In Kubernetes: Configure cert-manager's ClusterIssuer to explicitly request the matching audience string via serviceAccountRef.audiences: ["vault"].

#### Vault Role definition with explicit audience binding for Cluster A
```bash
vault write auth/k8s-cluster-a/role/cert-manager \
bound_service_account_names=cert-manager \
bound_service_account_namespaces=cert-manager \
audience="vault" \
policies=cert-manager-policy \
ttl=1h
```

### 4. Policy Reuse Without Scope Creep

Notice the architectural clean-up here: even though we have isolated Auth engines for each cluster, both clusters attach to the exact same Vault policies:
- eso-policy: Grants read access exclusively to kv/data/cnpg/*.
- cert-manager-policy: Grants certificate issuance permissions on pki/sign/cnpg-*.

Because permissions are granted based on verified roles (bound_service_account_names and namespaces) isolated per cluster path, we achieve complete authentication isolation without duplicating access control policy rules.

## SECTION 2: Cross-Cluster mTLS: The Central PKI Superpower

To set up physical streaming replication between a Primary PostgreSQL database in Cluster A and a Standby replica in Cluster B, CloudNativePG requires mutually authenticated TLS (mTLS). Every node connection across cluster boundaries must be encrypted, and both sides must verify each other's identity.

This is where traditional multi-cluster setups often fall apart.

### 1. Why Cluster-Local CAs Fail in Multi-Cluster Topologies

If you rely on local cert-manager CA issuers inside each individual cluster, Cluster A signs certificates using CA-A, and Cluster B signs certificates using CA-B.

THE CLUSTER-LOCAL CA ANTI-PATTERN:
```text
  Cluster A (Primary)                         Cluster B (Standby)
┌───────────────────────┐                   ┌───────────────────────┐
│ cert-manager          │                   │ cert-manager          │
│   └── Local CA-A      │                   │   └── Local CA-B      │
│         │             │                   │         │             │
│         ▼             │                   │         ▼             │
│ Primary Node Cert     │                   │ Standby Node Cert     │
│ (Signed by CA-A)      │                   │ (Signed by CA-B)      │
└───────────┬───────────┘                   └───────────┬───────────┘
            │                                           │
            └─────────────── X FAILS X ─────────────────┘
                   Replication Connection Rejected:
               "SSL error: certificate verify failed"
```

When the Standby replica in Cluster B attempts to open a replication stream to Cluster A, PostgreSQL rejects the handshake because Cluster A's trust store doesn't know or trust CA-B.
To bypass this without a central authority, operators are forced into manual anti-patterns:
- Extracting CA certificates from Cluster A and manually pasting them into Secret manifests in Cluster B.
- Re-syncing certificates every time a CA rotates.
- Storing unencrypted private keys in git repositories or CI/CD pipelines.

### 2. The Central Vault PKI Solution

By leveraging Vault's pki secrets engine, we eliminate the need to manually exchange or sync Root CA secrets across Kubernetes clusters.

Vault acts as the single source of cryptographic trust. Both clusters run their own local cert-manager instances, but neither cluster holds the Root CA private key. Instead, both cert-manager instances forward Certificate Signing Requests (CSRs) to Vault's central PKI engine via short-lived tokens.

✔ CENTRAL VAULT PKI PATTERN:
```text
                    ┌───────────────────────────────┐
                    │    HashiCorp Vault Server     │
                    │                               │
                    │   ┌───────────────────────┐   │
                    │   │   pki Secrets Engine  │   │
                    │   │   (cnpg-root-ca)      │   │
                    │   └───────────┬───────────┘   │
                    └───────────────┼───────────────┘
                                    │
               CSR Sign Requests    │    Signed TLS Certificates
               (via auth/k8s-a)     │    (via auth/k8s-b)
            ┌───────────────────────┴───────────────────────┐
            ▼                                               ▼
┌───────────────────────────────┐               ┌───────────────────────────────┐
│ k3d-cluster-a (Primary)       │               │ k3d-cluster-b (Standby)       │
│                               │               │                               │
│ cert-manager                  │               │ cert-manager                  │
│   └── cnpg-server-issuer      │               │   └── cnpg-server-issuer      │
│         │                     │               │         │                     │
│         ▼                     │               │         ▼                     │
│ Primary Node TLS Cert         │               │ Standby Node TLS Cert         │
│ (Signed by Vault Root CA)     │               │ (Signed by Vault Root CA)     │
└───────────────┬───────────────┘               └───────────────┬───────────────┘
                │                                               │
                └───────────── SUCCESSFUL mTLS ─────────────────┘
                      Both clusters inherently trust
                       the shared Vault Root CA!
```

Because both primary and standby certificates share the exact same root chain of trust (cnpg-root-ca), cross-cluster mTLS replication works out of the box without sharing a single private key between clusters.

### 3. PostgreSQL Identity Verification Requirements: Server vs. Client Roles

As explained in [part2](https://github.com/wyst69/cnpg-kubernetes-blueprint/blob/main/security/part2-secrets.md), CloudNativePG uses two distinct types of TLS certificates for database communication. In Vault, we model these requirements using two specialized PKI roles:
- Server Certificates (cnpg-server-role)
- Client Certificates (cnpg-client-role)

### 4. Summary of Benefits

Zero Private Key Exposure: Root and Intermediate CA private keys never leave the Vault memory boundary.

Automated Lifecycle Management: cert-manager automatically requests new leaf certificates prior to expiration without operator intervention.

Instant Trust Onboarding: Spinning up Cluster C, D, or E for DR or read-replicas requires zero CA sharing—new clusters trust the existing primary node as soon as they connect to Vault.


## SECTION 3: Step-by-Step Implementation Walkthrough

With our external Vault server configured and both k3d clusters online, we can now assemble the multi-cluster pipeline.

We will execute this in six clear steps:
- Step 1: Deploy Core Infrastructure (ClusterSecretStore & ClusterIssuer) on both clusters.
- Step 2: Store & Sync Application Credentials via Vault KV and ESO.
- Step 3: Issue TLS & mTLS Certificates for Cluster A.
- Step 4: Bootstrap Primary PostgreSQL in Cluster A.
- Step 5: Issue TLS & mTLS Certificates for Cluster B.
- Step 6: Bootstrap Standby Replica PostgreSQL in Cluster B using cross-cluster mTLS.

### Step 1: Deploy Infrastructure Backends on Both Clusters

First, tell each cluster's controllers how to authenticate with Vault. Notice that each cluster references its dedicated Vault mount path (k8s-cluster-a vs. k8s-cluster-b).

Apply to Cluster A:

```yaml
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: vault-backend
spec:
  provider:
    vault:
      server: "http://<HOST_IP>:8200"
      path: "kv"
      version: "v2"
      auth:
        kubernetes:
          mountPath: "k8s-cluster-a"
          role: "eso"
          serviceAccountRef:
            name: "external-secrets"
            namespace: "external-secrets"
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: cnpg-server-issuer
spec:
  vault:
    server: "http://<HOST_IP>:8200"
    path: "pki/sign/cnpg-server-role"
    auth:
      kubernetes:
        mountPath: "/v1/auth/k8s-cluster-a"
        role: "cert-manager"
        serviceAccountRef:
          name: "cert-manager"
          audiences: ["vault"]
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: cnpg-client-issuer
spec:
  vault:
    server: "http://<HOST_IP>:8200"
    path: "pki/sign/cnpg-client-role"
    auth:
      kubernetes:
        mountPath: "/v1/auth/k8s-cluster-a"
        role: "cert-manager"
        serviceAccountRef:
          name: "cert-manager"
          audiences: ["vault"]
```

Apply to Cluster B:

```yaml
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: vault-backend
spec:
  provider:
    vault:
      server: "http://<HOST_IP>:8200"
      path: "kv"
      version: "v2"
      auth:
        kubernetes:
          mountPath: "k8s-cluster-b"
          role: "eso"
          serviceAccountRef:
            name: "external-secrets"
            namespace: "external-secrets"
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: cnpg-server-issuer
spec:
  vault:
    server: "http://<HOST_IP>:8200"
    path: "pki/sign/cnpg-server-role"
    auth:
      kubernetes:
        mountPath: "/v1/auth/k8s-cluster-b"
        role: "cert-manager"
        serviceAccountRef:
          name: "cert-manager"
          audiences: ["vault"]
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: cnpg-client-issuer
spec:
  vault:
    server: "http://<HOST_IP>:8200"
    path: "pki/sign/cnpg-client-role"
    auth:
      kubernetes:
        mountPath: "/v1/auth/k8s-cluster-b"
        role: "cert-manager"
        serviceAccountRef:
          name: "cert-manager"
          audiences: ["vault"]
```
### Step 2: Store & Sync Application Credentials

Store the application database user credentials centrally in Vault:

```bash
docker exec -e VAULT_ADDR='http://127.0.0.1:8200' -e VAULT_TOKEN='root' external-vault \
  vault kv put kv/cnpg/app-user username="app_user" password="SuperSecretPassword123!"
```

Now, deploy an ExternalSecret resource on both clusters. ESO will reach into Vault's kv path and automatically materialize a native Kubernetes Secret (app-user-credentials):

```yaml
# app-user-cred.yaml

apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: app-user-credentials
  namespace: default
spec:
  refreshInterval: "1h"
  secretStoreRef:
    name: vault-backend
    kind: ClusterSecretStore
  target:
    name: app-user-credentials
    creationPolicy: Owner
  data:
    - secretKey: username
      remoteRef:
        key: cnpg/app-user
        property: username
    - secretKey: password
      remoteRef:
        key: cnpg/app-user
        property: password
```

```bash
kubectl apply -f app-user-cred.yaml --context k3d-cluster-a

kubectl apply -f app-user-cred.yaml --context k3d-cluster-b

# Output example x2:
externalsecret.external-secrets.io/app-user-credentials created
```

### Step 3: Create the certificates for CNPG cluster A

```yaml
# certificates-A.yaml

# 1. Server TLS Certificate (Requested from Vault via cert-manager)
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: cnpg-server-tls
  namespace: default
spec:
  secretName: cnpg-server-tls
  issuerRef:
    name: cnpg-server-issuer
    kind: ClusterIssuer
    group: cert-manager.io
  commonName: cnpg-primary.default.svc
  dnsNames:
      # Read-Write Service
    - cnpg-primary-rw.default.svc
    - cnpg-primary-rw.default.svc.cluster.local

    # Read-Only Service
    - cnpg-primary-ro.default.svc
    - cnpg-primary-ro.default.svc.cluster.local

    # Read Service
    - cnpg-primary-r.default.svc
    - cnpg-primary-r.default.svc.cluster.local

    # Wildcards for pod-to-pod communication
    - "*.default.svc"
    - "*.default.svc.cluster.local"
---
# 2. Server CA Secret (cert-manager fetches Vault CA into ca.crt)
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: cnpg-server-ca
  namespace: default
spec:
  secretName: cnpg-server-ca
  issuerRef:
    name: cnpg-server-issuer
    kind: ClusterIssuer
    group: cert-manager.io
  commonName: cnpg-primary.default.svc
---
# 3. Client CA Secret (cert-manager fetches Vault CA into ca.crt)
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: cnpg-client-ca
  namespace: default
spec:
  secretName: cnpg-client-ca
  issuerRef:
    name: cnpg-client-issuer
    kind: ClusterIssuer
    group: cert-manager.io
  commonName: cnpg-client-ca
---
# 4. Streaming Replica secret
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: cnpg-replication-cert
  namespace: default
spec:
  secretName: cnpg-replication-cert
  issuerRef:
    name: cnpg-client-issuer
    kind: ClusterIssuer
    group: cert-manager.io
  commonName: streaming_replica
  usages:
    - client auth
```

```bash
kubectl apply -f certificates-A.yaml --context k3d-cluster-a

# Output example:
certificate.cert-manager.io/cnpg-server-tls created
certificate.cert-manager.io/cnpg-server-ca created
certificate.cert-manager.io/cnpg-client-ca created
certificate.cert-manager.io/cnpg-replication-cert created
```

### ⚠️ SCOPE NOTE:

In this previous article [Cross-clusters streaming replica](https://github.com/wyst69/cnpg-kubernetes-blueprint/tree/main/database-clusters), we explored how to combine streaming replication with Barman Cloud and S3-compatible Object Storage to build a resilient Cross-clusters Bidirectional Streaming Replication.

For Part 3, our primary objective is solving the complex security plumbing: Multi-Cluster Vault Authentication and Cross-Cluster mTLS.

To keep our multi-cluster lab lightweight and focused entirely on the Vault identity contract, we intentionally opted for simple streaming replication without an object store.

In a full production environment, you must combine both patterns.

### Step 4: Bootstrap Primary Database (Cluster A)

Now we deploy the Primary CloudNativePG cluster in Cluster A.

```yaml
# cnpg-primary.yaml

apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: cnpg-primary
  namespace: default
spec:
  instances: 2
  
  # Point directly to the Secrets created by cert-manager above
  certificates:
    serverCASecret: cnpg-server-ca
    serverTLSSecret: cnpg-server-tls
    clientCASecret: cnpg-client-ca
    replicationTLSSecret: cnpg-replication-cert
  # Use Vault-synced credentials
  bootstrap:
    initdb:
      database: app_db
      owner: app_user
      secret:
        name: app-user-credentials

  storage:
    size: 1Gi
---
# 5. Expose Cluster A Primary to Cluster B via MetalLB
apiVersion: v1
kind: Service
metadata:
  name: cnpg-primary-lb
  namespace: default
spec:
  type: LoadBalancer
  loadBalancerIP: 172.18.250.10 # ADAPT to your own Load Balancer IP range
  ports:
    - port: 5432
      targetPort: 5432
  selector:
    cnpg.io/cluster: cnpg-primary
    cnpg.io/instanceRole: primary
```

```bash
kubectl apply -f cnpg-primary.yaml --context k3d-cluster-a

# check the status of the cluster
kubectl cnpg status cnpg-primary --context k3d-cluster-a
```

### Step 5: Create the certificates for CNPG cluster B 

To configure Cluster B as a physical standby replica, we need two components:

    Replication mTLS Certificate: A cert-manager Certificate object in Cluster B requesting a client cert with commonName: streaming_replica signed by Vault PKI.

    CNPG Standby Manifest: CNPG's externalClusters spec pointing directly to Cluster A's LoadBalancer IP (172.18.250.10).

```yaml
# certificates-B.yaml

# 1. Server TLS Certificate (Requested from Vault via cert-manager)
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: cnpg-server-tls
  namespace: default
spec:
  secretName: cnpg-server-tls
  issuerRef:
    name: cnpg-server-issuer
    kind: ClusterIssuer
    group: cert-manager.io
  commonName: cnpg-standby.default.svc
  dnsNames:
    - cnpg-standby-rw.default.svc
    - cnpg-standby-rw.default.svc.cluster.local
    - cnpg-standby-ro.default.svc
    - cnpg-standby-ro.default.svc.cluster.local
    - cnpg-standby-r.default.svc
    - cnpg-standby-r.default.svc.cluster.local
    - "*.default.svc"
    - "*.default.svc.cluster.local"
---
# 2. Server CA Secret (cert-manager fetches Vault CA into ca.crt)
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: cnpg-server-ca
  namespace: default
spec:
  secretName: cnpg-server-ca
  issuerRef:
    name: cnpg-server-issuer
    kind: ClusterIssuer
    group: cert-manager.io
  commonName: cnpg-standby.default.svc
---
# 3. Client CA Secret (cert-manager fetches Vault CA into ca.crt)
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: cnpg-client-ca
  namespace: default
spec:
  secretName: cnpg-client-ca
  issuerRef:
    name: cnpg-client-issuer
    kind: ClusterIssuer
    group: cert-manager.io
  commonName: cnpg-client-ca
---
# 4. Streaming Replica secret
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: cnpg-replication-cert
  namespace: default
spec:
  secretName: cnpg-replication-cert
  issuerRef:
    name: cnpg-client-issuer
    kind: ClusterIssuer
    group: cert-manager.io
  commonName: streaming_replica
  usages:
    - client auth
```

```bash
kubectl apply -f certificates-B.yaml --context k3d-cluster-b

# Output example:
certificate.cert-manager.io/cnpg-server-tls created
certificate.cert-manager.io/cnpg-server-ca created
certificate.cert-manager.io/cnpg-client-ca created
certificate.cert-manager.io/cnpg-replication-cert created
```

### Step 6: Bootstrap Standby Replica (Cluster B)
```yaml
# cnpg-standby.yaml

apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: cnpg-standby
  namespace: default
spec:
  instances: 2

  # 1. Bootstrap via streaming replication from Cluster A
  bootstrap:
    pg_basebackup:
      source: cnpg-primary

  # 2. Maintain continuous replication from Cluster A
  replica:
    enabled: true
    source: cnpg-primary

  # 3. Define the external connection to Cluster A Primary
  externalClusters:
    - name: cnpg-primary
      connectionParameters:
        host: 172.18.250.10 # <-- Replace with Cluster A's MetalLB / LoadBalancer IP
        user: streaming_replica
        dbname: postgres
        sslmode: verify-ca
      sslKey:
        name: cnpg-replication-cert
        key: tls.key
      sslCert:
        name: cnpg-replication-cert
        key: tls.crt
      sslRootCert:
        name: cnpg-replication-cert
        key: ca.crt

  # 4. Certificates for local Cluster B services (issued via Vault)
  certificates:
    serverCASecret: cnpg-server-ca
    serverTLSSecret: cnpg-server-tls
    clientCASecret: cnpg-client-ca
    replicationTLSSecret: cnpg-replication-cert

  storage:
    size: 1Gi
```

```bash
kubectl apply -f cnpg-standby.yaml --context k3d-cluster-b

# check the status of the cluster
kubectl cnpg status cnpg-standby --context k3d-cluster-b

# Output example:
Replica Cluster Summary
Name:                cnpg-standby
Namespace:           default
System ID:           7669424770503815193
PostgreSQL Image:    ghcr.io/cloudnative-pg/postgresql:18.4-system-trixie
Designated primary:  cnpg-standby-1
Source cluster:      cnpg-primary
Status:              Cluster in healthy state 
Instances:           2
Ready instances:     2

Certificates Status
Certificate Name       Expiration Date                Days Left Until Expiration
----------------       ---------------                --------------------------
cnpg-client-ca         2026-09-02 12:49:00 +0000 UTC  30.91
cnpg-replication-cert  2026-09-01 13:49:37 +0000 UTC  29.96
cnpg-server-ca         2026-09-02 12:49:00 +0000 UTC  30.91
cnpg-server-tls        2026-09-01 14:10:05 +0000 UTC  29.97

Continuous Backup status
Not configured

Unmanaged Replication Slot Status
No unmanaged replication slots found

Instances status
Name            Database Size  Current LSN  Replication role              Status  QoS         Manager Version  Node
----            -------------  -----------  ----------------              ------  ---         ---------------  ----
cnpg-standby-1                 0/8000060    Designated primary            OK      BestEffort  1.30.0           k3d-cluster-b-agent-0
cnpg-standby-2                 0/8000060    Standby (in Replica Cluster)  OK      BestEffort  1.30.0           k3d-cluster-b-server-0
```

┌─────────────────────────────────────────────────────────────────────────┐
│                     FAILOVER ADVANTAGE MATRIX                           │
├──────────────────────────┬──────────────────────┬───────────────────────┤
│ Operational Domain       │ Traditional Setup    │ Vault + ESO + CNPG    │
├──────────────────────────┼──────────────────────┼───────────────────────┤
│ Application Secrets      │ Manual Secret Sync   │ Synced via Vault KV   │
│ Database Passwords       │ Rotate/Update in DR  │ Unchanged in Vault    │
│ Server TLS Trust         │ Update Client Trust  │ Root CA Preserved     │
│ Client mTLS Identity     │ Re-issue Client Certs│ Valid across DCs      │
│ Downtime Recovery Time   │ Hours (High Risk)    │ Seconds (Low Risk)    │
└──────────────────────────┴──────────────────────┴───────────────────────┘

- Unchanged Application Credentials: The application workload running in Cluster B already uses the app-user-credentials secret managed by ESO. Because the password stored at kv/data/cnpg/app-user in Vault is identical, application pods in Cluster B connect immediately without configuration changes.

- Preserved mTLS Trust Chain: Client and server certificates in Cluster B are issued by Vault's central cnpg-root-ca. Connecting application microservices already trust this root chain, eliminating TLS handshake failures during failover.

🎯 Conclusion: Completing the Trilogy

Across this three-part series, we have progressed from fundamental cloud-native database patterns to an enterprise-grade disaster recovery architecture:

- Part 1: Understanding CloudNativePG fundamentals about role management.

- Part 2: Establishing the "Contract of Trust" inside a single cluster using Vault, External Secrets Operator, and cert-manager.

- Part 3: Scaling to multi-cluster topologies using external Vault authentication isolation, central PKI mTLS, and cross-cluster PostgreSQL replication.

By offloading secret life-cycle management and PKI authority to a centralized HashiCorp Vault instance, you decouple security policy from individual Kubernetes cluster lifecycles. The result is a resilient, automated, zero-trust database infrastructure capable of surviving full datacenter outages with zero manual key re-issuance.
