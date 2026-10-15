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
Pool connections use autocommit. Each write transaction commits before the response, so a read just after a write sees it.

## Client parity

The frontend and research agents use the same API. Each value that the frontend shows, and each rule that it applies, has an API operation.
Rules have versioned names. `GET /rules` describes them:

| Rule | Kind | Meaning |
|---|---|---|
| `period-agreement/1` | display | Target status for marker colours: `match` (relative period error < 0.01), `near` (< 0.05), `miss`, `signal` (no known planet and depth S/N > 100), `eb`, and `quiet`. It needs known-object labels. It is not a validation criterion. |
| `default-run/1` | selection | The analysis run with the most complete jobs. The newest run wins a tie. |
| `target-query/1` | query | The search grammar of `GET /targets` and the frontend search field. |
| `fold-window/1` | display | Window and bins of a fold view. A fold is a display calculation, not a stored result. |
| `bls-grid/1` | estimate | Estimated BLS period grid size of one light curve. |

The frontend repeats three calculations while the user types or drags: the target query, the fold, and the parameter checks.
`tests/vectors/` holds shared cases for these calculations. The API tests and the frontend tests both pass them.
`scripts/make_vectors.py` writes the vectors from the API code. A changed vector means a changed rule.
The frontend keeps a copy of the vectors in `vendor/api-vectors/`.

A later blind-campaign policy withholds known-object labels from some clients.
`GET /targets` then gives no label fields and no status, and refuses query fields that need labels.

## Endpoints

All paths start with `/api`. The interactive documentation is at `/api/docs`.

| Method and path | Result |
|---|---|
| `GET /health` | Database status. The status code is 503 when the database is unavailable. |
| `GET /methods` | Every method with its kind, scientific status, parameter schema, and defaults. |
| `GET /methods/{id}/{version}` | One method. |
| `GET /datasets` | Datasets with state, findings, last index time, and observation counts by state. |
| `POST /datasets/{id}/index` | Queues one `tess-spoc-index` job. The status code is 202. A queued or running index job for the dataset gives 409. |
| `GET /rules` | The named rules that clients apply. |
| `GET /observations` | Observations with `ra_deg`, `dec_deg`, and `tmag` from the header. Filters: `dataset_id`, `target_id`, `state`, `class` (matches `labels.class`: `planet_host`, `no_known_planet`, or `eclipsing_binary`), `limit` (at most 1000), `offset`. |
| `GET /targets` | Ready observations with cadence, sky position, magnitude, labels, the search values of one run, and the `period-agreement/1` status. Filters: `run_id`, `dataset_id`, `class`, `in_run`, and `q` (grammar `target-query/1`). `sort` names an attribute, with `-` for descending order. Default: `-depth_snr`. Null values sort last. `limit` (at most 5000), `offset`. A query error gives 422. Without `run_id`, the search values and the status are null, `run_values` is false, and a filter or sort on a run field gives 422. |
| `GET /observations/{id}` | One observation with header, labels, findings, and light-curve artifacts. |
| `POST /runs` | Creates a run and one queued job for each observation. The status code is 201. |
| `POST /runs/estimate` | Checks a run request as `POST /runs` does and writes nothing. Returns the full parameters and the work estimate. The estimate has the trial periods of each observation (rule `bls-grid/1`) and the trial limit. It also has the worker time, from the median rate of the newest 200 complete jobs of the method. A failed check gives the same 422 as `POST /runs`. |
| `GET /runs` | Runs, newest first, with job counts by state. |
| `GET /runs/default` | The default run (rule `default-run/1`). Filters: `dataset_id`, `method_id`, `method_version`. 404 when no analysis run has a complete job. |
| `GET /runs/{id}` | One run with parameters, inputs, and jobs. |
| `GET /runs/{id}/results` | Measurements by role, artifacts, diagnostics, resources, and software for each job. `observation_id` limits the response to one job. |
| `GET /runs/{id}/observations/{observation_id}/fold` | The stored flattened flux folded and binned (rule `fold-window/1`). Defaults: the stored BLS period and transit time. Options: `period`, `t0`, `window_hours`, `bin_hours`, `t_min`, `t_max`, and `points`. The box model shows only for the stored ephemeris. |
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
An error from an `x-constraints` rule also has `rule` and `paths`. Its `loc` is the path of the rule's first term.
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
`test_rules.py` checks the rules against `tests/vectors/`. `test_parity_api.py` checks the client-parity endpoints, including the SQL form of the target query against the vectors.

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
