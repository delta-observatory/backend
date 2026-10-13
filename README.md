# Delta Observatory API

This repository contains the stateless HTTP API for Delta Observatory.
The API reads and writes PostgreSQL records and reads artifact files.
It never calls a worker. Workers claim the jobs that the API inserts.
The current scope is the TESS vertical slice: the `tess-spoc-index` and `tess-bls` methods.

## Contracts

The API reads job and method-parameter schemas from `vendor/contracts/`.
`vendor/contracts/SOURCE` records the database repository commit of the copy.
`vendor/migrations/` holds the migrations of the same commit. The tests apply them.
Update both copies from a database commit or tag:

```bash
scripts/sync_contracts.sh <database-commit-or-tag>
```

Do not edit the vendored files by hand.
The API does not import code from `../database` at runtime.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | empty | PostgreSQL connection URI. |
| `DELTA_ARTIFACT_ROOT` | `/data/artifacts` | Artifact storage root. The API only reads it. |
| `DELTA_CONTRACTS_DIR` | `vendor/contracts` | Contract schema directory. |
| `DELTA_CORS_ORIGINS` | empty | Comma-separated origins that can call the API from a browser. |

The API keeps no job, run, or session state in process memory.
Any replica can serve any request.

## Endpoints

All paths start with `/api`. The interactive documentation is at `/api/docs`.

| Method and path | Result |
|---|---|
| `GET /health` | Database status. The status code is 503 when the database is unavailable. |
| `GET /methods` | Every method with its kind, scientific status, parameter schema, and defaults. |
| `GET /methods/{id}/{version}` | One method. |
| `GET /datasets` | Datasets with state, findings, last index time, and observation counts by state. |
| `POST /datasets/{id}/index` | Queues one `tess-spoc-index` job. The status code is 202. A queued or running index job for the dataset gives 409. |
| `GET /observations` | Observations. Filters: `dataset_id`, `target_id`, `state`, `label` (matches `labels.label`), `limit`, `offset`. |
| `GET /observations/{id}` | One observation with header, labels, findings, and light-curve artifacts. |
| `POST /runs` | Creates a run and one queued job for each observation. The status code is 201. |
| `GET /runs` | Runs, newest first, with job counts by state. |
| `GET /runs/{id}` | One run with parameters, inputs, and jobs. |
| `GET /runs/{id}/results` | Measurements by role, artifacts, diagnostics, resources, and software for each job. |
| `GET /jobs/{id}` | One job with its job and result documents. |
| `POST /jobs/{id}/cancel` | Cancels a queued job. Another state gives 409. |
| `GET /artifacts/{id}` | Artifact metadata. |
| `GET /artifacts/{id}/content` | The stored file. A `gzip` artifact has `Content-Encoding: gzip`. The ETag is the SHA-256. |

### Run requests

```json
{
  "method": {"id": "tess-bls", "version": 1},
  "parameters": {"search": {"maximum_period": 8.0}},
  "observation_ids": ["<uuid>"],
  "label": "optional text"
}
```

The API fills omitted parameters from the schema defaults.
It checks the complete parameter set against the method schema and the `x-constraints` rules.
Each observation must exist and have the state `ready`.
A failed check gives 422 with one `{loc, msg}` entry for each error. The API writes nothing.
`parameters_sha256` is the SHA-256 of the parameters as compact JSON with sorted keys.

The API writes these resource limits into each job document:

| Method | Time limit | Memory limit |
|---|---|---|
| `tess-bls` | 600 s | 2048 MiB |
| `tess-spoc-index` | 1800 s | 1536 MiB |

### Measurements

Each measurement has `role`, `name`, `value`, `unit`, `uncertainty`, and `quality_flags`.
`uncertainty` is `{"kind": "standard", "value": …}` or `{"kind": "unavailable", "reason": …}`.
A time value also has `time_scale` and `time_reference`.

## Tests

The tests use a temporary PostgreSQL 16 server from the `pgserver` package.
They apply `vendor/migrations/` to it. They do not use mocks for the database.

```bash
uv run pytest
```

## Build

`requirements.txt` holds the exact runtime versions with hashes. Regenerate it after a change to `pyproject.toml`:

```bash
uv lock
uv export --no-dev --no-emit-project --format requirements-txt -o requirements.txt
```

The image runs as uid 1000 and listens on port 8080.
Build it in the cluster with `kubernetes/scripts/build.sh backend`. The image name is `localhost/delta-backend:<tag>`.
