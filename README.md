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
| `DELTA_COHORTS_PATH` | `/data/experiments/cohorts.json` | The shared cohort register, mounted read-only. The cohort guard reads it. |

The API keeps no job, run, or session state in process memory.
Any replica can serve any request.
Pool connections use autocommit. Each write transaction commits before the response, so a read just after a write sees it.

## Authentication and permissions

Every endpoint except `GET /health` and the API description needs a bearer token: `Authorization: Bearer <token>`.
A missing, unknown, or disabled token gives 401. A missing permission gives 403.
Each token belongs to one client in the `client` table: a person, an agent, or a service. The API stores only the SHA-256 of a token.

| Permission | Allows |
|---|---|
| `read` | Every read endpoint, and `POST /runs/estimate`. |
| `run.submit` | `POST /runs`, and `POST /jobs/{id}/cancel` for the client's own runs. |
| `record.write` | `POST /campaigns/{id}/fix-decisions` for an agent of the campaign. Research records will also use it. |
| `acquire` | Acquisition of new observations. No endpoint uses it yet. |
| `identity.reveal` | `POST /campaigns/{id}/reveal`. |
| `admin` | Client and campaign management, access events, dataset indexing, and cancellation of any job. |

Richard decided that an agent never holds `acquire`, `identity.reveal`, or `admin`. The API and a database constraint both refuse it.
Each write records its client: `run.client_id` and `run.requested_by` (`client:<name>`), and `job.cancelled_by`.
Every agent belongs to one campaign (`client.campaign_id`). The next section gives the campaign rules.

Create the first client with a command next to the API. It prints the token once:

```bash
kubectl -n delta-observatory exec deploy/delta-api -- python -m delta_api.admin create-client --name richard --kind person --permissions all
```

That client creates other clients with `POST /clients`. An agent client needs `campaign_id`.
The command takes `--campaign <name>` for an agent.

## Campaigns

A campaign is the scope of agent work. It has a question, a blinding level, budgets, and a lifecycle.
`policy.py` applies the campaign rules to each request of an agent. A person or a service has no campaign rules.

The lifecycle has this order:

1. Richard creates the campaign (`POST /campaigns`) and the agent clients.
2. Richard approves the campaign file (`POST /campaigns/{id}/approve` with its SHA-256). Before the approval, an agent reads only its campaign.
3. The agent reads observation data and submits runs inside the budgets.
4. The agent or Richard fixes the decisions (`POST /campaigns/{id}/fix-decisions`). After this, the agent submits no new runs.
5. Richard records the reveal with a reason (`POST /campaigns/{id}/reveal`). Blinding then ends for the campaign.
6. Richard closes the campaign (`POST /campaigns/{id}/close`). After this, the agent reads no observation data and writes nothing.

Each step is valid only after the step before it. A step in the wrong state gives 409. The database checks the same order.

### Blinding

| Level | Withheld from the agent |
|---|---|
| `open` | Nothing. |
| `labels` | Known-object labels: `labels`, label classes, planet names, reference periods, period errors, the `period-agreement/1` status, and `reference_comparison` measurements and diagnostics. |
| `labels_identity` | The labels, and catalog identity: `target_id`, `relative_path`, `sha256`, sky position, identifying header keywords, and the source hash in `input_checks`. |

Richard decided that `labels_identity` is the default level.
A withheld value is null or absent, and each list or detail response names the withheld groups in `withheld`.
A filter, query field, text search, or sort on a withheld value gives 422.
A redacted `search_summary` or `input_checks` artifact comes back as uncompressed JSON.
Header keywords for vetting stay visible: sector, magnitude, stellar parameters, crowding, and noise values. They narrow the identity but do not give it.

### Cohort guard

The guard reads `experiments/cohorts.json`. An `evaluation` reservation protects its observations.
An observation matches when its `target_id` is an observation or group identity of the reservation.
It also matches when `mast:TESS/product/<relative_path>` is an observation identity.

For an agent, lists leave out protected observations and give the count in `protected_excluded`.
A read of a protected observation gives 403, and a run request with one gives 422.
Each refusal writes an `access_event` row with outcome `refused`. Each read by a person writes one with outcome `allowed`.
When the register is missing or invalid, agent requests that touch observations give 503. Person requests continue.

### Budgets

| Budget | Counts |
|---|---|
| `observations_read` | Distinct observations whose data the campaign read: details, results, folds, jobs, and artifact content. Lists do not count. |
| `jobs` | Jobs in the runs of the campaign. |
| `compute_seconds` | Worker seconds of finished jobs, plus estimates for waiting and new jobs at the median rate of the method. |
| `storage_bytes` | Bytes of the artifacts of the campaign jobs. |

A missing key means no limit. A request past a limit gives 403 and writes nothing.
`POST /runs/estimate` gives an agent the budget state after the run, and the budgets that the run would exceed.
`GET /campaigns/{id}` gives the limit, use, and remaining amount of each budget.

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

A campaign with blinding withholds known-object labels from its agents.
`GET /targets` then gives no label fields and no status, and refuses query fields that need labels.

## Endpoints

All paths start with `/api`. The interactive documentation is at `/api/docs`.

| Method and path | Result |
|---|---|
| `GET /health` | Database status. The status code is 503 when the database is unavailable. No token. |
| `GET /me` | The client of the token, with its permissions. |
| `GET /clients`, `POST /clients`, `POST /clients/{id}/disable` | Client management (`admin`). `POST /clients` returns the token once. |
| `GET /campaigns`, `GET /campaigns/{id}` | Campaigns with budget state and clients. An agent sees only its own campaign. |
| `POST /campaigns`, `POST /campaigns/{id}/approve`, `POST /campaigns/{id}/budgets`, `POST /campaigns/{id}/close` | Campaign management (`admin`). |
| `POST /campaigns/{id}/fix-decisions` | Fixes the decisions (`record.write` for an agent of the campaign, or `admin`). |
| `POST /campaigns/{id}/reveal` | Records the reveal with a reason (`identity.reveal`). |
| `GET /access-events` | Reads and refused reads of protected observations, newest first (`admin`). Filters: `campaign_id`, `outcome`, `limit`. |
| `GET /methods` | Every method with its kind, scientific status, parameter schema, and defaults. |
| `GET /methods/{id}/{version}` | One method. |
| `GET /datasets` | Datasets with state, findings, last index time, and observation counts by state. |
| `POST /datasets/{id}/index` | Queues one `tess-spoc-index` job (`admin`). The status code is 202. A queued or running index job for the dataset gives 409. |
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
| `POST /jobs/{id}/cancel` | Cancels a queued job of the client's own run, or any queued job with `admin`. Another state gives 409. |
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
`test_auth.py` checks tokens, permissions, the agent restriction, and actor records.
`test_campaigns.py` checks that blinding, the cohort guard, budgets, and the lifecycle stop prohibited agent actions.
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
