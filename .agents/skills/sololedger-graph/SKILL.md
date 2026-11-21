---
name: sololedger-graph
description: >-
  Runbook for SoloLedger Multi-Layer Graph Engineering (code AST dependency graph,
  runtime workflow auto-wiring, SQLite schema FK graph, StateGraph topology, and
  BigQuery Property Graph analytics/ADC setup). Use when auditing architectural
  graphs, inspecting workflow checkpoints, or provisioning BigQuery Property Graph.
---

# SoloLedger Graph Engineering Skill

SoloLedger pairs **Harness Engineering** with **Multi-Layer Graph Engineering** (`src/sololedger/graph_engineering.py`, `workflow_graph.py`, `audit_graph.py`, `bq_graph_exporter.py`).

## 1. Six Graph Engineering Invariants

Running `powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 graph` (or stage `[5/6] graph_engineering` inside `verify`) generates `logs/architecture_graph.json` and enforces:

1. **Zero Circular Module Imports**: No directed import cycle across `sololedger.*` modules.
2. **100% Test Reachability**: Every `sololedger.*` module (excluding `__init__.py`) is reachable directly or transitively from `tests/test_*.py`.
3. **Runtime Graph Auto-Wiring**:
   - `POST /api/v1/webhooks/sms` -> `run_transaction_workflow`
   - `POST /api/v1/webhooks/push` -> `run_transaction_workflow`
   - `POST /api/v1/webhooks/telegram` -> `sync_checkpoint_from_ledger`
   - `POST /api/v1/receipts/upload` -> `sync_checkpoint_from_ledger`
   - `POST /api/v1/exports/tax-pack` -> `run_pre_export_tax_audit`
4. **SQLite Schema & FK Integrity**: 5 core tables (`merchants`, `card_transactions`, `receipt_ocr_metadata`, `tax_classification_rules`, `workflow_checkpoints`) exist and all `FOREIGN KEY` targets are valid.
5. **StateGraph Reachability**: All 6 nodes (`ingest_parse`, `nts_and_rule_eval`, `llm_tax_inference`, `arithmetic_tax_validator`, `human_interrupt`, `commit_ledger`) are reachable from `entry_point`.
6. **BigQuery Property Graph Completeness**: `sql/bigquery_property_graph.sql` defines all 5 node tables, 4 edge tables, `CREATE PROPERTY GRAPH`, and at least 3 named `@gql_query` blocks.

## 2. Commands & Endpoints

- **CLI Architecture Graph Audit**:
  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 graph
  ```
- **API Inspection**:
  - `GET /api/v1/system/engineering-graph` (pass `?full=true` for complete node/edge graph)
  - `GET /api/v1/analytics/bigquery-status` (check local ADC / `gcloud` readiness)
  - `POST /api/v1/analytics/bigquery-provision` (create BigQuery dataset + Property Graph DDL)
- **One-Click Local BigQuery Setup**:
  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts/setup_gcp_bigquery.ps1 -InstallSdk -ProjectId <YOUR_GCP_PROJECT_ID>
  ```
