# Hybrid Backups: Deploy & Verify

## Prerequisites

- A CSI driver with `VolumeSnapshotClass` support already installed, and at
  least one `VolumeSnapshotClass` created for it (`nfs-csi-snapclass` here —
  MAY DIFFER IN YOUR ENVIRONMENT, check with `kubectl get volumesnapshotclass`).
- An S3-compatible bucket reachable from the cluster (MinIO in this example),
  with a `minio-creds` Secret already created in the `default` namespace
  (`ACCESS_KEY_ID` / `ACCESS_SECRET_KEY` keys).
- The barman-cloud CNPG plugin installed in the cluster.

## Step 1: Deploy the ObjectStore

```bash
kubectl apply -f objectStore.yaml
```

## Step 2: Deploy the cluster

```bash
kubectl apply -f pg-cluster.yaml
```

Check both backup paths are actually wired up:

```bash
kubectl cnpg status pg-cluster -n default
```

Should show the `barman-cloud.cloudnative-pg.io` plugin as the configured
WAL archiver, and continuous archiving succeeding.

## Step 3: Deploy the two backup schedules

**Gotcha confirmed live:** `ScheduledBackup.spec.schedule` is NOT standard
5-field crontab syntax — CNPG uses `robfig/cron`, which requires a leading
seconds field (6 fields total: `sec min hour day month weekday`). A 5-field
schedule like `"0 1 * * *"` applies without error but triggers a `kubectl`
admission warning ("Schedule parameter may not have the right number of
arguments") and won't fire as intended — write `"0 0 1 * * *"` instead.

```bash
kubectl apply -f scheduled-backups.yaml
```

```bash
kubectl get scheduledbackup -n default
```

Both `pg-cluster-barman-daily` and `pg-cluster-snapshot-4h` should be
present. Nothing backs up immediately — they fire on their own cron
schedule. To test either path without waiting:

```bash
kubectl cnpg backup pg-cluster -n default --method=plugin --plugin-name=barman-cloud.cloudnative-pg.io
kubectl cnpg backup pg-cluster -n default --method=volumeSnapshot
```

```bash
kubectl get backup -n default
kubectl get volumesnapshot -n default
```

Confirm a `VolumeSnapshot` object was created for each PVC the snapshot
backup covers (PGDATA, and PG_WAL/tablespaces if separate classes were set).

## Step 4: Deploy the snapshot pruner

```bash
kubectl apply -f pruner-account.yaml
kubectl apply -f cluster-retention-map.yaml
kubectl apply -f cron-pruner.yaml
```

`cluster-retention-map.yaml` ships with a `pg-cluster` entry
(`retention: 4h`, `minRedundancy: 1`) matching the schedule above — adjust
before using on a different cluster name or window.

## Step 5: Verify pruning actually works

The `CronJob` runs at 02:00 daily by default — don't wait a day to find out
if it's configured correctly. Trigger it manually:

```bash
kubectl create job --from=cronjob/cnpg-pruner cnpg-pruner-manual-test -n default
kubectl logs -n default job/cnpg-pruner-manual-test
```

Expect one line per resource kind (`volumesnapshot`, `backup`) per
non-suspended cluster in the retention map, reporting how many valid
(non-expired) resources it found and whether anything was old enough to
prune. With a fresh cluster and `minRedundancy: 1`, expect "nothing pruned
yet" until more than one snapshot exists past the 4h window.

To force an actual prune for testing, temporarily create backups older
than the retention window, or lower `retention` in the ConfigMap to
something shorter than your test cadence, re-apply, and re-run the manual
Job.

## What NOT to expect

- **Barman-cloud pruning is NOT manual** — it happens automatically,
  driven by the ObjectStore's own `retentionPolicy` reconciliation. There is
  no equivalent `kubectl create job` trick for it; give it time on its own
  schedule, or check `kubectl describe objectstore minio-store -n default`
  for its current state.
- **The pruner CronJob never touches barman-managed `Backup` CRs** — its
  RBAC only grants access to `volumesnapshots` and `backups`, and its query
  filters `Backup` objects to `method: volumeSnapshot` specifically. If a
  barman-cloud backup looks like it wasn't pruned, that's by design — check
  the ObjectStore's `retentionPolicy` instead, not this job.
