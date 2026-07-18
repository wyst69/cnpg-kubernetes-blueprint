# 📚 Part 1: GitOps-First Postgres Roles (The CNPG 1.30 Way)

Stop treating your database roles like a secondary configuration step. 

If you are still running manual `CREATE ROLE` commands via interactive SQL shells or stuffing database users directly into a monolithic `Cluster` manifest, you are building an operational bottleneck. 

With the release of **CloudNativePG 1.30**, PostgreSQL roles have finally graduated to first-class Kubernetes citizens. The introduction of the standalone **`DatabaseRole` CRD** decouples cluster-level platform management from application-level schema ownership. Application teams can now define, deploy, and own their database credentials declaratively inside their own repositories—without ever touching the core database spec.

In this article, we explore the architecture of declarative role management, design a hybrid mTLS/Password split authentication pattern, dissect role inheritance, and investigate the silent operational traps of cascading drops.

---

## 👥 The Machine vs. Human Split Authentication Model

When designing a zero-trust database platform, a one-size-fits-all authentication model fails. We draw a strict boundary between automated machine workloads and human operators:

| Identity Type | Target Client | Authentication Protocol | Lifecycle Model |
| :--- | :--- | :--- | :--- |
| **Workloads** | Microservices, CronJobs, Workers | **mTLS (Passwordless)** via Client Certificates | **Fully Automated:** Issued, rotated, and expired dynamically by the CNPG operator. |
| **Humans** | DBAs, Analytics, Dev Debugging | **Passwords** via `SCRAM-SHA-256` | **Static Persistent:** Managed out-of-band, secured via Vault/ESO, and deployed as native secrets. |

---

## 🔗 The Role Inheritance Pattern (`inRoles`)

PostgreSQL does not actually have separate concepts for "users" and "groups"—it only has **`ROLES`**. A role is a single object that can optionally be given the ability to log in (`LOGIN`) or act as a container for permissions (a "group").

To design a clean GitOps database platform, we leverage **Role Inheritance** to separate connection identities from table-level permissions:

```
                  ┌──────────────────────┐
                  │   app_read_write     │  <-- Schema Privilege Group
                  │ (No LOGIN / Owns DDL)│
                  └──────────┬───────────┘
                             │
                  Granted via│ "inRoles" (Inherit: true)
                             ▼
                  ┌──────────────────────┐
                  │     app_billing      │  <-- Workload Login Role
                  │  (LOGIN / mTLS Cert) │
                  └──────────────────────┘
```

### How the CRD maps to PostgreSQL under the hood:
When you configure the `inRoles` array inside a `DatabaseRole` manifest, the CNPG operator dynamically translates your YAML declaration into real-time database queries:

* **Reconciliation:** The operator executes `GRANT app_read_write TO app_billing;` inside the engine.
* **State Drift Sync:** If a role is removed from the `inRoles` list in Git, the operator automatically issues a `REVOKE` command to align the active database state back to your source of truth.
* **Implicit Inheritance:** By default, CNPG roles are created with `inherit: true`. This means the workload login role automatically assumes all permissions assigned to its parent group roles without having to explicitly execute `SET ROLE` inside the application code.

### The Clean Separation of Concerns:
This architecture cleanly divides operational boundaries:
1. **The Platform Team** uses GitOps to define the core database clusters and the parent group templates (e.g., a globally defined `pg_read_all_data` or a custom `app_read_write` role).
2. **The Application Developers** only need to deploy their namespace-scoped `DatabaseRole` manifest with their login identity (e.g., `app_billing`) and reference the parent group in their `inRoles` block.
3. **The Migration Pipeline** (Liquibase/Flyway) assigns the physical DDL permissions (`GRANT SELECT, INSERT ON ALL TABLES...`) directly to the parent group role, leaving the ephemeral workload identities clean and decoupled.

### 💡 Platform Architecture Rule: The "NOLOGIN" Group Role

When creating parent group roles (like `app_read_write`), you should always set `login: false`. Because these roles act strictly as permission containers and cannot establish active database connections, they do not require credentials of any kind. 

The CNPG validating webhook is smart: the `passwordSecret` and `clientCertificate` fields in the `DatabaseRole` CRD are fully optional. When `login: false` is declared, the operator skips credential generation entirely, allowing you to build lightweight, passwordless access structures.

---

## 🔌 Pattern A: The Zero-Trust Workload (Passwordless mTLS)

For applications, we can completely eliminate static passwords. Under CNPG 1.30, configuring the `clientCertificate` block instructs the operator to automatically issue and renew a private TLS client certificate signed by the cluster's client CA. 

### 1. The `DatabaseRole` Manifest

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: DatabaseRole
metadata:
  name: app-billing-role      # <--- This Kubernetes Metadata Name dictates the Secret name!
  namespace: database-prod
spec:
  cluster:
    name: pg-main
  databaseRoleReclaimPolicy: delete  # Declarative cleanup
  roles:
    - name: app_billing       # <--- This is the actual SQL role name inside Postgres
      login: true
      comment: "Transactional microservice workload"
      disablePassword: true   # <--- Forcefully disables password authentication in the DB
      clientCertificate:
        enabled: true
      inRoles:
        - app_read_write      # <-- Inherits permissions from the schema group role
```

> 📌 **Strict Naming Convention Rule:** 
> The CNPG operator enforces a strict, non-configurable naming convention for the generated client TLS secret. It takes the **Kubernetes resource metadata name** (here, `app-billing-role`) and appends `-client-cert` to it. It does *not* use the internal SQL role name (`app_billing`). Therefore, your application deployment must mount a secret named exactly: **`app-billing-role-client-cert`**.

### 2. Consuming the Client Certificate in the Application

To connect to the database, the application must mount this secret and configure its client driver. 

> ⚠️ **PostgreSQL Client Private Key Security Rule:**
> PostgreSQL client drivers (like `libpq`) strictly enforce file permissions on private keys. If the private key file is readable by other users (i.e., anything other than `0600`), the connection will be rejected. You *must* configure `defaultMode: 256` (`0600` octal) on the Kubernetes volume mount.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: billing-service
  namespace: database-prod
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: app
          image: internal-registry/billing-service:v1.2.0
          env:
            # We build the DSN using SSL verification parameters
            - name: DATABASE_URL
              value: "host=pg-main-rw.database-prod.svc.cluster.local user=app_billing dbname=billing sslmode=verify-full sslcert=/etc/ssl/postgres/tls.crt sslkey=/etc/ssl/postgres/tls.key sslrootcert=/etc/ssl/postgres/ca.crt"
          volumeMounts:
            - name: db-certs
              mountPath: /etc/ssl/postgres
              readOnly: true
      volumes:
        - name: db-certs
          secret:
            secretName: app-billing-role-client-cert
            defaultMode: 0600 # Hex/Decimal equivalent for owner-only read/write
```

---

## 🔑 Pattern B: The Human Operator (SCRAM-SHA-256 Passwords)

Since humans cannot comfortably manage ephemeral mTLS files on their local machines, they access the system using secure passwords. For maximum safety, CNPG 1.30 enforces `SCRAM-SHA-256` hashing. 

### 1. The DBA `DatabaseRole` Manifest

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: DatabaseRole
metadata:
  name: dba-human-role
  namespace: database-prod
spec:
  cluster:
    name: pg-main
  databaseRoleReclaimPolicy: retain  # Keep the database identity intact even if YAML is deleted
  roles:
    - name: lead_dba
      login: true
      comment: "Human administrator access role"
      passwordSecret:
        name: dba-bootstrap-password # Basic-auth format containing username/password
      connectionLimit: 5             # Prevent concurrent session exhaustion
```

---

## ⚠️ The GitOps Trap: Cascading Drops & Hanging Finalizers

In a pure GitOps workflow, your reflex might be to set `databaseRoleReclaimPolicy: delete` across all environments. The dream is perfect cleanup: deleting the YAML deletes the role inside PostgreSQL.

However, **PostgreSQL has a strict safety gate:**

> 🚫 Postgres will reject a `DROP ROLE <name>` command if that role **owns any database objects** (tables, schemas, databases) or **holds direct privileges** on them.

If you delete a `DatabaseRole` CRD with a reclaim policy of `delete` while it still owns objects:
1. The `DROP ROLE` query fails inside the Postgres engine.
2. The CNPG operator's reconciliation loop fails to resolve.
3. The Kubernetes resource gets stuck in a permanent **`Terminating`** state because the operator's finalizer cannot complete.
4. Your GitOps controller (like ArgoCD or Flux) starts reporting a degraded/failed synchronization loop.

### 🛠️ The Safe Drop Procedure

To safely execute a declarative delete on an existing role, you must clean up its relational footprint inside the database *before* removing the Kubernetes manifest. Log into your target databases and execute:

Option 1: you want to keep the objects owned by the role
```sql
-- 1. Reassign ownership of all objects (tables, schemas) to a successor role
REASSIGN OWNED BY app_billing TO principal_owner;

-- 2. Revoke any remaining privileges (like SELECT/INSERT grants)
DROP OWNED BY app_billing;
```
Option 2: you want to delete the objects owned by the role
```sql
-- 1. Delete all objects and revoke any remaining privileges (like SELECT/INSERT grants)
DROP OWNED BY app_billing;
```

Once those commands complete across your database landscape, the operator will process the `DatabaseRole` deletion instantly and cleanly.

### ⚠️ Crucial Edge Case: Database Ownership

If the role you want to drop is the actual owner of an entire database, a standard `REASSIGN OWNED BY` command executed on a default connection will fail to clean inner-database objects. This is because a database container is a **global object**, while the schemas and tables inside it are **local objects**.

To safely evict a role that owns a database, follow this exact sequence:

1. **Transfer Global Container Ownership** (Run from any connection, like the `postgres` db):
```sql
ALTER DATABASE corporate_prod_db OWNER TO deployment_admin;
```

Then apply the previous procedure at the database level

---

## 🔭 Up Next: Part 2 — The Cross-Cluster Vault Integration

We now have declarative, namespace-scoped GitOps roles. But what happens in a Multi-Cluster Disaster Recovery scenario? 

The local client and server CAs managed by the CNPG operator are scoped to individual Kubernetes instances. If we replicate our data to a standby disaster-recovery cluster (`pg-dr`), how do we prevent automated local certificate rotation from silently destroying the replication boundary?

In **Part 2**, we will solve this enterprise constraint by moving cert-manager to a shared **HashiCorp Vault PKI Secrets Engine**, and coordinating our database passwords using the **External Secrets Operator (ESO)**. Stay tuned.