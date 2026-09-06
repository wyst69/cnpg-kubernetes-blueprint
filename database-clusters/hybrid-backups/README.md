# Hybrid Backups: Volume Snapshots + Barman-Cloud

> Want to try this yourself? See [`PLAYBOOK.md`](PLAYBOOK.md) for the
> deploy steps and how to verify pruning actually works.

## 🧠 Why One Backup Method Isn't Enough

CloudNativePG supports two fundamentally different backup mechanisms on the
same `Cluster`, and the temptation is to pick one and move on. Neither one
alone gives you a complete story.

### 1. The Retention Trap (Why Snapshot-Only Doesn't Work)

* **The Assumption:** volume snapshots are fast, so why not skip the
  object store entirely and back up with snapshots alone?
* **The Constraint:** CNPG's `retentionPolicy` field — the one mechanism
  that automatically prunes old backups **and WALs** — is explicit about
  its own scope, straight from the CRD:

  > "RetentionPolicy is the retention policy to be used for backups and
  > WALs... It's currently only applicable when using the BarmanObjectStore
  > method."

* **The Impact:** a snapshot-only cluster still needs continuous WAL
  archiving underneath it — to make each snapshot consistent (the
  online/hot backup path replays a handful of WAL records to reach a
  consistent state) and to allow PITR between snapshots. But nothing ever
  prunes that WAL archive on its own. Left alone, it grows forever.
* **The Same Gap Hits the Snapshots Themselves:** `VolumeSnapshot` objects
  and their `Backup` CRs (`method: volumeSnapshot`) have no
  `retentionPolicy` equivalent at all — CNPG creates them on schedule and
  then never touches them again. Nothing expires, nothing gets pruned.

### 2. RTO vs. Durability — Not the Same Axis

* **Volume snapshots:** near-instant restore (a block-level clone of the
  PVC) — the right tool when the goal is minimizing downtime. But the
  snapshot lives in the same storage backend as the cluster it protects.
  Lose that backend, lose the snapshots with it. Realistically kept for
  hours to a few days, not months.
* **Barman-cloud:** restore means fetching a base backup and replaying WAL
  from object storage — slower, but the object store is a genuinely
  separate failure domain, cheap enough to keep for months or years, and
  it's the only side CNPG actually manages retention for.
* **The takeaway:** these two aren't competing on the same axis. Snapshots
  buy you RTO. Barman-cloud buys you durability and retention depth. A
  serious backup strategy needs both, not a choice between them.

### 3. Closing the Snapshot Retention Gap

Since CNPG won't prune snapshot-method backups, something else has to. The
pieces in this folder do exactly that, on a schedule, per cluster:

* **`cluster-retention-map.yaml`** — a `ConfigMap` naming each cluster's
  retention window (`retention: 4h`, `2d`, ...) and a `minRedundancy` floor
  so pruning never drops below N valid backups regardless of age. A
  `suspend` flag skips a cluster entirely without deleting the entry.
* **`pruner-account.yaml`** — a `ServiceAccount` + `Role` scoped to exactly
  what the job needs: `get/list/watch/delete` on `volumesnapshots`, and the
  same on `backups` — deliberately narrow, not cluster-admin.
* **`cron-pruner.yaml`** — a daily `CronJob` reading the retention map,
  and for each non-suspended cluster: deleting `VolumeSnapshot` objects and
  `Backup` CRs older than the configured window, but only once at least
  `minRedundancy` valid (non-expired) ones already exist. The `Backup`
  query is filtered to `method: volumeSnapshot` specifically — barman's own
  `Backup` CRs are already covered by the ObjectStore's `retentionPolicy`
  and must never be touched by this job.

### 4. Activating Both on One Cluster

Both methods live on the same `Cluster` resource without conflict — one
under `.spec.plugins` (barman-cloud), one under `.spec.backup.volumeSnapshot`:

```yaml
spec:
  plugins:
    - name: barman-cloud.cloudnative-pg.io
      isWALArchiver: true
      parameters:
        barmanObjectName: minio-store

  backup:
    volumeSnapshot:
      className: nfs-csi-snapclass   # <-- must already exist as a VolumeSnapshotClass
```

The one hard constraint: `volumeSnapshot.className` must reference a
`VolumeSnapshotClass` that already exists in the cluster — CNPG doesn't
create it for you, and without it the snapshot side simply can't take a
backup. `className` is also the default for every PVC type (PGDATA,
tablespaces) unless overridden individually via `tablespaceClassName` /
`walClassName`.

Scheduling each method is two separate `ScheduledBackup` resources
(`method: plugin` for barman-cloud, `method: volumeSnapshot` for
snapshots) — see `scheduled-backups.yaml`. A realistic split: snapshots
every few hours for fast recovery, barman-cloud daily for long-term,
off-cluster retention.

*Snapshots give you speed. Barman-cloud gives you depth. Retention only
happens automatically on one side of that — the other side needs the
pruner in this folder, or it just keeps growing.*
