## 📝 Preamble

This article covers a lot of dense ground. Complete YAML manifests are kept in separate files within the companion repository.

`Scope Check:`
- This is not a tutorial on how to install or administer HashiCorp Vault. We will define the required Vault prerequisites (policies, auth methods, and PKI roles), but not how to provision them, as that varies widely depending on your tooling (Terraform, OpenTofu, Vault CLI, or GUI).
- We also assume that ESO (External Secrets Operator) and cert-manager have already been deployed. What will be explained is how to "configure" them to manage CNPG secrets with a Vault.

For the core walkthrough, we assume Vault is deployed inside the same Kubernetes cluster as CloudNativePG —our lab environment.
In part 3, we will pivot to a production pattern: shifting to an external, centralized Vault instance and exploring how cross-cluster secrets and mTLS certificates are managed in the real world.

---

## 📋 Prerequisites: The Vault "Contract of Trust"

Before we configure Kubernetes to pull secrets and sign certificates, your HashiCorp Vault instance must be configured with three key pillars: Access (Who), Engines (What), and Policies (Permitted Actions).

No matter how you deploy or manage Vault, verify that these four prerequisites are met:
### 1. **Enabled Secrets Engines**

Your Vault instance must have two specific engines active:

- A KV (Key-Value) Version 2 Engine: To securely store your role passwords (e.g., mounted at kv/).

- A PKI (Private Key Infrastructure) Secrets Engine: Configured with a Root or Intermediate Certificate Authority (CA) to sign TLS certificates (e.g., mounted at pki/).

⚠️ The PKI Dual-Role Requirement:
Within the pki/ engine, you must configure two distinct Vault PKI roles to accommodate PostgreSQL's mTLS certificate model:

- cnpg-server-role (Server TLS): Configured with allow_any_name=false and restricted to Kubernetes hostnames (e.g., allowed_domains="svc,svc.cluster.local", allow_subdomains=true).

- cnpg-client-role (Client & Replication mTLS): Configured with allow_any_name=true and enforce_hostnames=false to permit arbitrary non-DNS identities like streaming_replica and database user names (analytics_user).

#### Why two PKI roles?
🚨 Why Client Certificates Break Without allow_any_name=true

Vault evaluates every single request against allowed_domains. It checks the Common Name (CN) and Subject Alternative Names (SANs):

1. Server Certificates:

    - cert-manager requests: ha-postgres-1.database-prod.svc.cluster.local
    - Vault checks: Is this a subdomain of .svc.cluster.local? Yes!

2. Replication & Client Certificates:
    - cert-manager requests: commonName: "streaming_replica" (or "analytics_user", "postgres")
    - Vault checks: Is streaming_replica a subdomain of .svc.cluster.local? No!

If allow_any_name=false is set, Vault rejects the replication certificate with this exact error:
``` Code: 400. Errors: common name "streaming_replica" not allowed by this role```

🏆 The Enterprise Solution: The 2-Role Pattern

Instead of compromising security by turning on allow_any_name=true across the board, production environments separate Server issuance from Client issuance into two distinct Vault PKI roles.

1. Server Role (cnpg-server-role)

Strictly locked to Kubernetes DNS hostnames. Disallows arbitrary names.

```bash
vault write pki/roles/cnpg-server-role \
    allowed_domains="svc,svc.cluster.local" \
    allow_subdomains=true \
    allow_any_name=false \
    enforce_hostnames=true \
    require_cn=false \
    max_ttl=720h
```

2. Client & Replication Role (cnpg-client-role)

Allows non-DNS strings (like streaming_replica or analytics_user) as valid Common Names for mTLS user authentication.

```bash
vault write pki/roles/cnpg-client-role \
    allow_any_name=true \
    enforce_hostnames=false \
    require_cn=true \
    max_ttl=720h
```

### 2. **Enabled Kubernetes Auth Method**

Vault must be configured to trust your Kubernetes cluster. The Kubernetes Auth Method must be enabled (typically at the default path kubernetes) and configured with your cluster’s API address so Vault can verify incoming service account tokens.

### 3. **The Security Policies (What is allowed)**

We enforce the Principle of Least Privilege by defining two distinct Vault ACL policies:

The Secrets Policy (e.g., eso-policy): Grants permission to read the Postgres credentials path only.
```
path "kv/data/cnpg/*" {
    capabilities = ["read"]
}
```

The PKI Policy (e.g., cert-manager-policy): Grants permission to request and sign TLS certificates from your PKI role.
```
path "pki/sign/*" {
    capabilities = ["create", "update"]
}

path "pki/issue/*" {
  capabilities = ["create", "update"]
}
```

### 4. **The Auth Roles (Who is allowed)**

Finally, these policies must be mapped to your Kubernetes workloads via Vault Roles under the Kubernetes Auth Method:

- The eso Role: Binds the eso-policy to the external-secrets Service Account in the external-secrets namespace.

- The cert-manager Role: Binds the cert-manager-policy to the cert-manager Service Account in the cert-manager namespace.


In this guide, we assume these four pieces are in place.

---CloudNativePG & HashiCorp Vault (Part 3): Centralized Vault, Multi-Cluster Topologies, and Cross-Cluster mTLS


## 🤝 The Authentication Handshake (Under the Hood)

When you apply the ExternalSecret, a highly secure, zero-password chain reaction triggers between your cluster and Vault:

```text
[ ESO Pod ]                                [ K8s API Server ]                           [ Vault Server ]
     │                                              │                                           │
     │  1. Read local SA JWT Token                  │                                           │
     │─────────────────────────────┐                │                                           │
     │                             │                │                                           │
     │◄────────────────────────────┘                │                                           │
     │                                              │                                           │
     │  🔒 TLS Handshake: ESO verifies Vault using 'vault-ca-secret' (ca.crt)                   │
     │─────────────────────────────────────────────────────────────────────────────────────────►│
     │                                                                                          │
     │  2. POST /v1/auth/kubernetes/login (JWT, Role: "eso") (ENCRYPTED)                        │
     │─────────────────────────────────────────────────────────────────────────────────────────►│
     │                                              │                                           │
     │                                              │  3. TokenReview API Call (Is JWT valid?)  │
     │                                              │◄──────────────────────────────────────────│
     │                                              │                                           │
     │                                              │  4. Response: "Yes, it's external-secrets"│
     │                                              │──────────────────────────────────────────►│
     │                                              │                                           │
     │                                              │  5. Validate boundaries, issue Vault Token│
     │                                              │  with 'eso-policy'                        │
     │                                              │◄──────────────────────────────────────────┘
     │                                              │                                           │
     │  6. Return Temporary Vault Token             │                                           │
     │◄─────────────────────────────────────────────────────────────────────────────────────────│
     │                                                                                          │
     │  7. GET /v1/kv/data/cnpg/dev/admin (Using Vault Token)                                   │
     │─────────────────────────────────────────────────────────────────────────────────────────►│
     │                                                                                          │
     │  8. Returns decrypted payload                                                            │
     │◄─────────────────────────────────────────────────────────────────────────────────────────│
     │                                                                                          │
     │  9. Create native K8s Secret 'dba-bootstrap-password'                                    │
     │─────────────────────────────┐                                                            │
     │                             │                                                            │
     │◄────────────────────────────┘                                                            │
```

- The Token Read: The external-secrets operator pod runs with a specific Kubernetes ServiceAccount (external-secrets). Kubernetes automatically mounts this account's JSON Web Token (JWT) inside the pod filesystem at /var/run/secrets/kubernetes.io/serviceaccount/token.

- The Login Request: ESO reads this local JWT token file and sends an HTTP POST request to Vault's /v1/auth/kubernetes/login endpoint, stating: "I want to log in using the Kubernetes auth engine, and I claim to match the role eso."

- The Verification Loop: Vault does not trust the pod blindly. It takes that JWT and forwards it back to your Kubernetes API Server's TokenReview API. It asks: "Is this a real, active token inside your cluster, and who does it belong to?"

- The Validation: Your K8s control plane validates the cryptographic signature of the token and replies to Vault: "Yes, that token is active and belongs to service account external-secrets in namespace external-secrets."

- The Boundary Check: Vault checks the eso role configuration you saved in the GUI. It confirms that the incoming service account name and namespace match the bounds you configured, and then maps the token to the eso-policy.

- The Read Session: Vault issues a temporary Vault Token back to ESO. ESO uses this token to safely execute a GET request to kv/data/postgres/dba, parses the JSON payload, base64-encodes the username and password, and writes them to your target Kubernetes namespace as a standard secret.

The exact same mechanism happens for cert-manager:

```text
[ Cert-Manager ]                           [ K8s API Server ]                           [ Vault Server ]
     │                                              │                                           │
     │  1. Read local SA JWT Token                  │                                           │
     │─────────────────────────────┐                │                                           │
     │                             │                │                                           │
     │◄────────────────────────────┘                │                                           │
     │                                              │                                           │
     │  🔒 TLS Handshake: ESO verifies Vault using 'vault-ca-secret' (ca.crt)                   │
     │─────────────────────────────────────────────────────────────────────────────────────────►│
     │                                              │                                           │
     │  2. POST /v1/auth/kubernetes/login (JWT, Role: "cert-manager") (ENCRYPTED)               │
     │─────────────────────────────────────────────────────────────────────────────────────────►│
     │                                              │                                           │
     │                                              │  3. TokenReview API Call (Is JWT valid?)  │
     │                                              │◄──────────────────────────────────────────│
     │                                              │                                           │
     │                                              │  4. Response: "Yes, it is cert-manager"   │
     │                                              │──────────────────────────────────────────►│
     │                                              │                                           │
     │                                              │  5. Validate boundaries, issue Vault Token│
     │                                              │  with 'cert-manager-policy'               │
     │                                              │◄──────────────────────────────────────────┘
     │                                              │                                           │
     │  6. Return Temporary Vault Token             │                                           │
     │◄─────────────────────────────────────────────────────────────────────────────────────────│
     │                                                                                          │
     │  7. Generate Local Private Key & CSR                                                     │
     │─────────────────────────────┐                                                            │
     │                             │                                                            │
     │◄────────────────────────────┘                                                            │
     │                                                                                          │
     │  8. POST /v1/pki/sign/cnpg-certs (Send CSR)                                              │
     │─────────────────────────────────────────────────────────────────────────────────────────►│
     │                                                                                          │
     │  9. Vault signs CSR with Root/Intermediate CA and returns Certificate + Chain            │
     │◄─────────────────────────────────────────────────────────────────────────────────────────│
     │                                                                                          │
     │  10. Create native K8s Secret (tls.crt, tls.key, ca.crt)                                 │
     │─────────────────────────────┐                                                            │
     │                             │                                                            │
     │◄────────────────────────────┘                                                            │
```

---

## PHASE 1: Create the links between ESO, Cert-manager and the Vault ##

`IMPORTANT NOTE:` There are two versions of `PHASE 1` depending on whether you connect to the `Vault` using HTTP or HTTPS

## PHASE 1: HTTP ##

## Step 1: ESO (External Secret Operator) => create the ClusterSecretStore
```yaml
# cluster-secret-store-http.yaml

apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: vault-backend
spec:
  provider:
    vault:
      server: "http://vault.vault.svc.cluster.local:8200" # Change to your Vault service URL
      path: "kv"       # Points to your KV mount path
      version: "v2"    # Tells ESO to auto-inject the "/data/" segment
      auth:
        kubernetes:
          mountPath: "kubernetes"
          role: "eso" # Matches the Role you created in the Vault
          serviceAccountRef:
            name: "external-secrets" # This service account is automatically created when installing ESO
            namespace: "external-secrets"
```

```bash
kubectl apply -f cluster-secret-store-http.yaml

kubectl get clustersecretstore
# Output example:
NAME            AGE     STATUS   CAPABILITIES   READY
vault-backend   7d22h   Valid    ReadWrite      True
```

## Step 2: cert-manager => create the 2 issuers
```yaml
# cnpg-server-issuer-http.yaml

apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: cnpg-server-issuer
spec:
  vault:
    # 1. Target our internal Vault Service address
    server: "http://vault.vault.svc.cluster.local:8200"
    
    # 2. Point to the specific signing role we mapped in the prerequisites
    path: "pki/sign/cnpg-server-role"
    
    # 3. Pass through our Unified Gateway using K8s Auth
    auth:
      kubernetes:
        mountPath: "/v1/auth/kubernetes"
        role: "cert-manager" # Matches the role in your prerequisites
        serviceAccountRef:
          name: "cert-manager"
```
```yaml
# cnpg-client-issuer-http.yaml

apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: cnpg-client-issuer
spec:
  vault:
    # 1. Target our internal Vault Service address
    server: "http://vault.vault.svc.cluster.local:8200"
    
    # 2. Point to the specific signing role we mapped in the prerequisites
    path: "pki/sign/cnpg-client-role"
    
    # 3. Pass through our Unified Gateway using K8s Auth
    auth:
      kubernetes:
        mountPath: "/v1/auth/kubernetes"
        role: "cert-manager" # Matches the role in your prerequisites
        serviceAccountRef:
          name: "cert-manager"
```

```bash
kubectl apply -f cnpg-server-issuer-http.yaml
kubectl apply -f cnpg-client-issuer-http.yaml

kubectl get clusterissuer
# Output example:
NAME                 READY   AGE
cnpg-client-issuer   True    2m17s
cnpg-server-issuer   True    3s
```

## PHASE 1: HTTPS ##

### Step 1: Create a secret for the Vault CA public key ###

#### Fetch the certicate ####
```bash
# Extract the active CA from vault-tls-secret (the name may differ)
kubectl get secret vault-tls-secret -n vault -o jsonpath='{.data.ca\.crt}' | base64 -d > vault-ca.crt
```

#### Create the secret ####
```bash
# Create the secret for cert-manager & ESO
kubectl create secret generic vault-ca-secret --from-file=ca.crt=vault-ca.crt -n cert-manager

# Check its creation
kubectl get secret vault-ca-secret -n cert-manager
```

### Step 2: ESO (External Secret Operator) => create the ClusterSecretStore
```yaml
# cluster-secret-store-https.yaml

apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: vault-backend
spec:
  provider:
    vault:
      server: "https://vault.vault.svc.cluster.local:8200" # Change to your Vault service URL
      path: "kv"       # Points to your KV mount path
      version: "v2"    # Tells ESO to auto-inject the "/data/" segment
      caProvider:
        type: Secret
        name: vault-ca-secret
        namespace: cert-manager
        key: ca.crt
      auth:
        kubernetes:
          mountPath: "kubernetes"
          role: "eso"  # Matches the Role you created in the Vault
          serviceAccountRef:
            name: "external-secrets" # This service account is automatically created when installing ESO
            namespace: "external-secrets"
```

```bash
kubectl apply -f cluster-secret-store-https.yaml

kubectl get clustersecretstore
# Output example:
NAME            AGE     STATUS   CAPABILITIES   READY
vault-backend   7d22h   Valid    ReadWrite      True
```

## Step 3: cert-manager => create the 2 issuers
```yaml
# cnpg-server-issuer-https.yaml

apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: cnpg-server-issuer
spec:
  vault:
    # 1. Target our internal Vault Service address
    server: "https://vault.vault.svc.cluster.local:8200"
    
    # 2. Point to the specific signing role we mapped in the prerequisites
    path: "pki/sign/cnpg-server-role"
    
    # 3. Trust Vault's HTTPS listener using the CA secret we created
    caBundleSecretRef:
      name: vault-ca-secret
      key: ca.crt

    # 4. Pass through our Unified Gateway using K8s Auth
    auth:
      kubernetes:
        mountPath: "/v1/auth/kubernetes"
        role: "cert-manager" # Matches the role in your prerequisites
        serviceAccountRef:
          name: "cert-manager"
```
```yaml
# cnpg-client-issuer-https.yaml

apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: cnpg-client-issuer
spec:
  vault:
    # 1. Target our internal Vault Service address
    server: "https://vault.vault.svc.cluster.local:8200"
    
    # 2. Point to the specific signing role we mapped in the prerequisites
    path: "pki/sign/cnpg-client-role"
    
    # 3. Trust Vault's HTTPS listener using the CA secret we created
    caBundleSecretRef:
      name: vault-ca-secret
      key: ca.crt

    # 4. Pass through our Unified Gateway using K8s Auth
    auth:
      kubernetes:
        mountPath: "/v1/auth/kubernetes"
        role: "cert-manager" # Matches the role in your prerequisites
        serviceAccountRef:
          name: "cert-manager"
```

```bash
kubectl apply -f cnpg-server-issuer-https.yaml
kubectl apply -f cnpg-client-issuer-https.yaml

kubectl get clusterissuer
# Output example:
NAME                 READY   AGE
cnpg-client-issuer   True    2m17s
cnpg-server-issuer   True    3s
```

## PHASE 2: Create secrets for a new CNPG cluster ##

`NOTE 1:` from now on, there will be no difference whether we use HTTP or HTTPS

`NOTE 2:` in all the following manifests, we will be working in a namespace called `dev`

We will replace the default user `app` by `admin` which is defined as `an administrator for all CNPG clusters in a specific namespace`

### 1. The Single Namespace Admin Secret

#### Step 1: create the secret in the `vault`
There are several ways of doing it depending on your environment.

It must respect the following:
- the path to the secret `kv/cnpg/dev/admin`
- username="admin"
- password="YourSuperSecretPassword123!" <= The password can be changed

#### Step 2: create the external secret
```yaml
# cnpg-dev-admin-es.yaml

apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: cnpg-dev-admin-es
  namespace: dev
spec:
  refreshInterval: "1h" # ESO will check Vault every hour for password rotations
  secretStoreRef:
    name: vault-backend
    kind: ClusterSecretStore
  target:
    name: cnpg-dev-admin # This is the actual K8s Secret CNPG will use
    template:
      type: kubernetes.io/basic-auth
  data:
    - secretKey: username
      remoteRef:
        key: cnpg/dev/admin # The path inside your "kv" mount
        property: username
    - secretKey: password
      remoteRef:
        key: cnpg/dev/admin
        property: password
```

```bash
kubectl apply -f cnpg-dev-admin-es.yaml

kubectl get externalsecret cnpg-dev-admin-es -n dev
# Should show READY: True

kubectl get secret cnpg-dev-admin -n dev
# Should exist and have type: kubernetes.io/basic-auth
```

### 2. Request TLS Certificates from cert-manager

```yaml
# pg-cluster-certs.yaml

# 1. Server Certificate (for internal cluster communication & client connection)
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: pg-cluster-server-cert
  namespace: dev
spec:
  secretName: pg-cluster-server-tls
  issuerRef:
    name: cnpg-server-issuer
    kind: ClusterIssuer
  dnsNames:
    # Read-Write Service
    - pg-cluster-rw.dev.svc
    - pg-cluster-rw.dev.svc.cluster.local

    # Read-Only Service
    - pg-cluster-ro.dev.svc
    - pg-cluster-ro.dev.svc.cluster.local

    # Read Service
    - pg-cluster-r.dev.svc
    - pg-cluster-r.dev.svc.cluster.local

    # Wildcards for pod-to-pod communication
    - "*.pg-cluster.dev.svc"
    - "*.pg-cluster.dev.svc.cluster.local"
---
# 2. Streaming Replication Client Certificate
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: pg-cluster-replication-cert
  namespace: dev
spec:
  secretName: pg-cluster-replication-tls
  commonName: streaming_replica        # ◄ Strict CN requirement
  issuerRef:
    name: cnpg-client-issuer
    kind: ClusterIssuer
```

```bash
kubectl apply -f pg-cluster-certs.yaml

kubectl get certificate -n dev
# Output example:
NAME                          READY   SECRET                       AGE
pg-cluster-replication-cert   True    pg-cluster-replication-tls   4s
pg-cluster-server-cert        True    pg-cluster-server-tls        4s

kubectl get secrets -n dev
# Output example:
NAME                         TYPE                       DATA   AGE
cnpg-dev-admin               kubernetes.io/basic-auth   2      48m
pg-cluster-replication-tls   kubernetes.io/tls          3      20m
pg-cluster-server-tls        kubernetes.io/tls          3      119s
```

### 2. Create the instance

```yaml
# pg-cluster.yaml

apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pg-cluster
  namespace: dev
spec:
  instances: 3

  # 1. Use the ESO-managed admin user/password from Vault
  bootstrap:
    initdb:
      database: admindb           # Initial database name
      owner: admin                # Set 'admin' as DB owner
      secret:
        name: cnpg-dev-admin

  # 2. Wire up cert-manager TLS certificates
  certificates:
    serverTLSSecret: pg-cluster-server-tls
    serverCASecret: pg-cluster-server-tls
    clientCASecret: pg-cluster-replication-tls
    replicationTLSSecret: pg-cluster-replication-tls

  storage:
    size: 1Gi
```

```bash
kubectl apply -f pg-cluster.yaml

# Check the cluster status and, particularly, the replication status
kubectl cnpg status pg-cluster -n dev

# Output example:
Cluster Summary
Name                     dev/pg-cluster
System ID:               7667191731521269781
PostgreSQL Image:        ghcr.io/cloudnative-pg/postgresql:18.4-system-trixie
Primary instance:        pg-cluster-1
Primary promotion time:  2026-07-27 12:59:53 +0000 UTC (1m4s)
Status:                  Cluster in healthy state 
Instances:               3
Ready instances:         3
Size:                    128M
Current Write LSN:       0/6000060 (Timeline: 1 - WAL File: 000000010000000000000006)

Continuous Backup not configured

Streaming Replication status
Replication Slots Enabled
Name          Sent LSN   Write LSN  Flush LSN  Replay LSN  Write Lag  Flush Lag  Replay Lag  State      Sync State  Sync Priority  Replication Slot
----          --------   ---------  ---------  ----------  ---------  ---------  ----------  -----      ----------  -------------  ----------------
pg-cluster-2  0/6000060  0/6000060  0/6000060  0/6000060   00:00:00   00:00:00   00:00:00    streaming  async       0              active
pg-cluster-3  0/6000060  0/6000060  0/6000060  0/6000060   00:00:00   00:00:00   00:00:00    streaming  async       0              active

Instances status
Name          Current LSN  Replication role  Status  QoS         Manager Version  Node
----          -----------  ----------------  ------  ---         ---------------  ----
pg-cluster-1  0/6000060    Primary           OK      BestEffort  1.30.0           k803
pg-cluster-2  0/6000060    Standby (async)   OK      BestEffort  1.30.0           k801
pg-cluster-3  0/6000060    Standby (async)   OK      BestEffort  1.30.0           k802

# Try to connect to the cluster as `admin`
kubectl cnpg psql pg-cluster -n dev -- -h 127.0.0.1 -U admin -d admindb  # you'll be prompted for the password you used in your external secret
```

## PHASE 2: Create additional users with `DatabaseRole` ##

Here, we will create two users:
- `human-user` with a password
- `app-user` with mTLS

#### Step 1: create the secret in the `vault` for `human-user`
There are several ways of doing it depending on your environment.

It must respect the following:
- the path to the secret `kv/cnpg/dev/human-user`
- username="human-user"
- password="HumanUserSecurePassword456!" <= The password can be changed

#### Step 2: create the external secret
```yaml
# human-user-es.yaml

apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: human-user-es
  namespace: dev
spec:
  refreshInterval: "1h"
  secretStoreRef:
    name: vault-backend
    kind: ClusterSecretStore
  target:
    name: human-user
  data:
    - secretKey: password
      remoteRef:
        key: cnpg/dev/human-user
        property: password
    - secretKey: username
      remoteRef:
        key: cnpg/dev/human-user
        property: username
```

```bash
kubectl apply -f human-user-es.yaml

kubectl get externalsecret human-user-es -n dev
# Should show READY: True

kubectl get secret human-user -n dev
# Should exist and have type: kubernetes.io/basic-auth
```

#### Step 3: create the `DatabaseRole` for `human-user`
```yaml
# human-user-dr.yaml

apiVersion: postgresql.cnpg.io/v1
kind: DatabaseRole
metadata:
  name: human-user
  namespace: dev
spec:
  cluster:
    name: pg-cluster
  name: human-user
  login: true
  passwordSecret:
    name: human-user
```

```bash
kubectl apply -f human-user-dr.yaml

# Check it's creation
kubectl get databaserole -n dev
# Ouput example. Note: you might have to wait a few seconds for the reconciliation to happen. It might appear as applied "false" with an error message at first. 
NAME         AGE   CLUSTER      PG NAME      APPLIED   MESSAGE
human-user   78s   pg-cluster   human-user   true

# Check the connection to the cluster
kubectl cnpg psql pg-cluster -n dev -- -h 127.0.0.1 -U human-user -d admindb  # you'll be prompted for the password you used in your external secret
```

### Step 5: create the certificate for `app-user`
```yaml
# app-user-cert.yaml

apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: app-user-cert
  namespace: dev
spec:
  secretName: app-user-client-tls
  commonName: app-user                  # Must match DB user name
  issuerRef:
    name: cnpg-client-issuer
    kind: ClusterIssuer
```

```bash
kubectl apply -f app-user-cert.yaml

kubectl get certificate -n dev
# Output example
NAME                          READY   SECRET                       AGE
app-user-cert                 True    app-user-client-tls          6s
pg-cluster-replication-cert   True    pg-cluster-replication-tls   64m
pg-cluster-server-cert        True    pg-cluster-server-tls        64m
```

### Step 5: create the `DatabaseRole` for `app-user`
```yaml
# app-user-dr.yaml

apiVersion: postgresql.cnpg.io/v1
kind: DatabaseRole
metadata:
  name: app-user
  namespace: dev
spec:
  cluster:
    name: pg-cluster
  name: app-user
  login: true
```

```bash
kubectl apply -f app-user-dr.yaml

# Check it's creation
kubectl get databaserole -n dev
# Ouput example. Note: you might have to wait a few seconds for the reconciliation to happen. It might appear as applied "false" with an error message at first. 
NAME         AGE   CLUSTER      PG NAME      APPLIED   MESSAGE
app-user     8s    pg-cluster   app-user     true      
human-user   13m   pg-cluster   human-user   true      
```

Checking the connection will require some additional work. You will also need a local installation of `postgresql-client`

First, extract the certificates to your local path:

```bash
# 1. Extract the Server CA (to verify the PostgreSQL server certificate)
kubectl get secret pg-cluster-server-tls -n dev -o jsonpath='{.data.ca\.crt}' | base64 -d > ca.crt

# 2. Extract the Client Certificate & Key for app-user
kubectl get secret app-user-client-tls -n dev -o jsonpath='{.data.tls\.crt}' | base64 -d > tls.crt
kubectl get secret app-user-client-tls -n dev -o jsonpath='{.data.tls\.key}' | base64 -d > tls.key

# 3. Secure the key permissions (psql requires 0600 or it will refuse to run)
chmod 0600 tls.key
```

Then, add some rules in pg_hba.conf:

```yaml
# pg-cluster-hba.yaml

apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pg-cluster
  namespace: dev
spec:
  instances: 3

  postgresql:
    pg_hba:
      # 1. Require mTLS (cert auth) for app-user on the 'app' database
      - hostssl all app-user all cert
      # 2. Require password (scram-sha-256) for human-user on the 'app' database
      - hostssl all human-user all scram-sha-256

  # 1. Use the ESO-managed admin user/password from Vault
  bootstrap:
    initdb:
      database: admindb           # Initial database name
      owner: admin                # Set 'admin' as DB owner
      secret:
        name: cnpg-dev-admin

  # 2. Wire up cert-manager TLS certificates
  certificates:
    serverTLSSecret: pg-cluster-server-tls
    serverCASecret: pg-cluster-server-tls
    clientCASecret: pg-cluster-replication-tls
    replicationTLSSecret: pg-cluster-replication-tls

  storage:
    size: 1Gi
```

```bash
kubectl apply -f pg-cluster-hba.yaml
```

In another terminal, create a port-forwarding:

```bash
kubectl port-forward -n dev svc/pg-cluster-rw 5432:5432
```

Finally, you should be able to connect:
```bash
psql "host=pg-cluster-rw.dev.svc.cluster.local hostaddr=127.0.0.1 port=5432 dbname=admindb user=app-user sslmode=verify-full sslcert=tls.crt sslkey=tls.key sslrootcert=ca.crt"
```

That's it for Part 2. I hope you enjoyed it. Stay tuned for Part 3.