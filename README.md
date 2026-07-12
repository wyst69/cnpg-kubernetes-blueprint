# Cloud-Native PostgreSQL Enterprise Blueprint

This repository serves as a production-ready reference architecture for running stateful database workloads on Kubernetes, with a heavy focus on **PostgreSQL** and the **CloudNativePG (CNPG)** operator.

## 🏗️ Architecture Overview
The goal of this platform blueprint is to treat databases as a modern, self-service infrastructure product. It covers:
- **Orchestration:** Declarative CNPG cluster management.
- **Security:** GitOps-friendly secret injection via External Secrets Operator (ESO) and HashiCorp Vault.
- **Observability:** Advanced telemetry integration with Prometheus/Grafana and external APM tools (Dynatrace/OpenTelemetry).

## 📁 Repository Structure
- `/01-operators`: Infrastructure operator bootstrap.
- `/02-security`: Secure credential management workflows.
- `/03-database-clusters`: Highly available Postgres topologies.
- `/04-observability`: Real-time monitoring rules and telemetry manifests.
