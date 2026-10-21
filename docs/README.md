# Data Shell Documentation

## Authoritative References

| Document | Purpose |
|---|---|
| [production/CONFIGURATION_REFERENCE.md](production/CONFIGURATION_REFERENCE.md) | **Single source of truth** for all environment variables, CLI options, API endpoints, roles, capabilities, and certificate fields |
| [production/OPERATIONS_RUNBOOK.md](production/OPERATIONS_RUNBOOK.md) | Startup, health checks, backup, restore, recovery, monitoring, incident response, rollback |
| [production/DEPLOYMENT_VERIFICATION.md](production/DEPLOYMENT_VERIFICATION.md) | Executable procedure to verify a deployment in the target environment |
| [production/RELEASE_VALIDATION_PACKAGE.md](production/RELEASE_VALIDATION_PACKAGE.md) | Index and release-gate matrix for target-host qualification |
| [production/GENERATION_CONTRACT.md](production/GENERATION_CONTRACT.md) | The publication commit point, the reader contract, and what is explicitly **not** claimed |
| [production/INCIDENT_RESPONSE.md](production/INCIDENT_RESPONSE.md) | Incident runbooks: publication failure, unreconciled ledger, DB outage, disk exhaustion, tampering, credential compromise, tenant isolation, failed deployment |
| [production/BENCHMARK_PROCEDURE.md](production/BENCHMARK_PROCEDURE.md) | Reproducible capacity procedure with the measured results |
| [production/BACKUP_RESTORE_DRILL.md](production/BACKUP_RESTORE_DRILL.md) | Backup/restore drill record, measured recovery time, and the target drill |
| [production/SECURITY_REVIEW_PACKAGE.md](production/SECURITY_REVIEW_PACKAGE.md) | Package prepared for an independent security review |
| [production/RESIDUAL_RISK_REGISTER.md](production/RESIDUAL_RISK_REGISTER.md) | Residual risks with severity, consequence, evidence gap, and owner dependency |
| [production/RELEASE_CHECKLIST.md](production/RELEASE_CHECKLIST.md) | Pilot release gates, acceptance evidence, ownership, remaining blockers |
| [production/VERIFICATION_REPORT.md](production/VERIFICATION_REPORT.md) | Exact commands, test results, exit codes, and evidence limitations |
| [THREAT_MODEL.md](THREAT_MODEL.md) | Assets, trust boundaries, threats, and mitigations |
| [SETTLEMENT.md](SETTLEMENT.md) | Settlement architecture, commercial contract schema, pricing, acceptance |
| [SETTLEMENT_OPERATIONS.md](SETTLEMENT_OPERATIONS.md) | Settlement CLI/API reference, billing, disputes, production checklist |

## Guides

| Document | Purpose |
|---|---|
| [SCALING.md](SCALING.md) | The five scaling surfaces, per-deployment profiles, and how to re-tune with environment variables |
| [SDK.md](SDK.md) | Regenerated Python/TypeScript clients, pinned OpenAPI Generator workflow, client versioning truth table |
| [FIRST_RUN.md](FIRST_RUN.md) | 5-minute quickstart from install to first verified delivery |
| [EVALUATION.md](EVALUATION.md) | Adoption path, tested quickstart, integration patterns, controlled-pilot checklist, known limitations |
| [../TUTORIAL.md](../TUTORIAL.md) | End-to-end tutorial from contract authoring to enforcement |
| [../examples/README](../examples/README.md) | Example contracts, quickstart, and samples |

## Integrations

| Document | Purpose |
|---|---|
| [../integrations/airflow/README.md](../integrations/airflow/README.md) | Apache Airflow operators and hooks |

## Historical Reference

| Document | Purpose |
|---|---|
| [DESIGN_RECORD.md](DESIGN_RECORD.md) | Why P0 fixes and release-engineering changes were designed this way (historical) |
| [archive/PRODUCT_VALIDATION.md](archive/PRODUCT_VALIDATION.md) | Prior product validation workstream findings (2026-10-17, superseded) |

## Demo

| Document | Purpose |
|---|---|
| [demo.gif](demo.gif) | 60-second walkthrough: refusal, publish, tamper detection, recovery |
