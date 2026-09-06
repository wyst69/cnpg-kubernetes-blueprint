# Database Clusters

Production-grade CloudNativePG cluster architectures, each in its own folder
with manifests, a written breakdown, and a step-by-step playbook.

- [`cross-clusters-streaming-replica/`](cross-clusters-streaming-replica/README.md) —
  cross-datacenter main/DR topology with synchronous streaming replication
  and an object-store backstop for WAL retention.
- [`hybrid-backups/`](hybrid-backups/README.md) —
  combining volume snapshots (fast recovery) with barman-cloud object-store
  backups (long-term retention).
