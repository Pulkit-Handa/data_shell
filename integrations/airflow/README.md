# Data Shell Airflow Provider

Apache Airflow operators that gate a DAG on [Data Shell](https://github.com/) boundary
enforcement. *Bad data never lands, and never leaves.*

Two operators are provided:

| Operator | Transport | Best for |
| --- | --- | --- |
| `DataShellEnforceOperator` | Gateway HTTP API | Evidence-sized files, remote control plane |
| `DataShellLocalEnforceOperator` | Local `datashell` CLI | Multi-gigabyte files, air-gapped workers |

Both return the terminal ledger record (status, row counts, footing, artifact
paths), so a DAG can branch on the outcome.

## Install

```bash
pip install ".[tests]"        # from integrations/airflow
# or, once published:
pip install datashell-airflow
```

The package depends only on `apache-airflow`. The HTTP client itself uses the
standard library, so it can be unit-tested without Airflow installed.

## Configure a connection

Create an Airflow connection (kind **HTTP**) named `datashell_default`:

| Field | Value |
| --- | --- |
| Host | `datashell.internal` (or `https://datashell.example.com`) |
| Schema | `https` |
| Port | `443` |
| Password | your tenant API key |
| Extra | `{"verify_tls": true}` |

## Gate a DAG on a local file

```python
from datetime import datetime

from airflow import DAG
from datashell_airflow import DataShellLocalEnforceOperator

with DAG("vendor_settlements", start_date=datetime(2026, 1, 1), schedule="@daily") as dag:
    enforce = DataShellLocalEnforceOperator(
        task_id="enforce_boundary",
        contract="/etc/datashell/vendor_settlements.json",
        source="/mnt/landing/settlements_{{ ds }}.csv",
        target="/mnt/curated/settlements.parquet",
        tenant="finance",
        fail_on_rejection=True,   # fail the task if the boundary rejects the run
    )
```

## Gate a DAG on the gateway API

```python
from datashell_airflow import DataShellEnforceOperator

enforce = DataShellEnforceOperator(
    task_id="enforce_remote",
    contract_name="vendor-settlements",
    file_path="/mnt/landing/settlements.csv",
    datashell_conn_id="datashell_default",
    fail_on_rejection=True,
)
```

## Semantics

* A **REJECTED** run means the declared stop condition fired. The task raises
  only when `fail_on_rejection=True`; otherwise downstream tasks can inspect
  `status` and route the rejected batch.
* An **infrastructure failure** (non-zero CLI exit other than `2`, or an HTTP
  error) always fails the task.
* Enforcement is a write gate: rejected records are never published, and the
  signed certificate records the exact reason.

## Development

```bash
python -m pytest tests/          # runs without Airflow; operator tests skip
```
