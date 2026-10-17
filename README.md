# Delta Observatory API

This repository contains the stateless HTTP API for Delta Observatory.
The API reads and writes PostgreSQL records and reads artifact files.
It never calls a worker. Workers claim the jobs that the API inserts.
The current scope is the TESS vertical slice: the `tess-spoc-index` and `tess-bls` methods.
The API also keeps the research records of agent work: candidates, assessments, decisions, notes, and agent tasks.

## Contracts

The API reads job, method-parameter, and research-record schemas from `vendor/contracts/`.
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
| `record.write` | Research-record writes: candidates, assessments, decisions, notes, and agent tasks. `POST /campaigns/{id}/fix-decisions` for an agent of the campaign. |
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

## Research records

A client writes a record as a document of its contract in `vendor/contracts/records/`, for example `candidate/1`.
The API checks the document against the schema and fills the schema defaults. It adds the writer, the time, and the campaign.
The `contract` field names the version. Another version gives 422.
Records do not change. A correction is a new record. An assessment or a decision names the record that it corrects in `supersedes`.

| Record | Meaning |
|---|---|
| Candidate | A possible object or event: observations, evidence, kind, tier, summary, and an optional label. It holds no measured values. |
| Assessment | One status on the `evidence` or `identity` axis, with basis, reason, and evidence. |
| Decision | One advisory judgment: question, judge, model, packet checksum, policy, judged evidence status, recommended action, and reason. It sets no status and schedules nothing. |
| Note | Free text about one or more records. A note takes the campaign of its subjects. |
| Agent task | A unit of agent work in a campaign, with a lease. |

The status of a candidate follows rule `candidate-status/1`: the newest assessment on each axis gives it.
Without an assessment, the evidence status is `unreviewed` and the identity status is `unchecked`.
Each candidate response gives `evidence_status` and `identity_status` with the assessment and its basis.

Measurements, model judgments, evidence status, and identity status stay apart.
A candidate refers to `measurement` rows and does not copy their values. `GET /candidates/{id}` resolves the referenced measurements, artifacts, and jobs.
A decision with judge `model` is a model judgment. Its scores are judgments, not uncertainties or probabilities.
An agent writes a judgment as a decision. It gives an evidence status only with an assessment of basis `model_judgment` that cites the decision.

### References

A reference names a platform record by `kind` and `id`, or an external file by `uri`, `sha256`, and `bytes`.
The kinds are `campaign`, `observation`, `run`, `job`, `measurement`, `artifact`, `candidate`, `assessment`, `decision`, `note`, and `agent_task`.
The API refuses a reference to a record that does not exist, and an artifact reference whose `sha256` is not the stored SHA-256.
A record of a campaign refers only to records of the same campaign or to records without a campaign. The database checks the same rule.
A `platform` candidate needs a measurement or a job in its evidence. A `lab` candidate can use external files only. An external file is a proposal, not platform evidence.

### Campaign rules for records

| Rule | API behaviour |
|---|---|
| Scope | An agent writes and reads only the records of its own campaign. Another campaign's record is "not found". A record without a campaign is not visible to agents. |
| Approval | An agent writes records only in an approved campaign that is not closed. |
| Judgments | An agent writes decisions with judge `model` and assessments with basis `model_judgment` only. An identity assessment from an agent gives 403: identity comparison is a worker method or a person's review. |
| Decision freeze | After `fix-decisions`, a candidate, a decision, or an evidence assessment of the campaign gives 409 for every writer. Identity assessments, notes, and tasks continue. |
| Close | After the close, a record of the campaign gives 409, except a person's note. The close cancels the queued and claimed tasks of the campaign. |
| Blinding | When a campaign withholds labels, its agents see no identity assessments and no identity status, and a filter on them gives 422. A reference to a `reference_comparison` measurement or an identity assessment gives 422. A record that refers to such a value is hidden from the agents. The reveal ends this. |
| Writer's blinding | Each decision records in `withheld_from_writer` the values that blinding withheld from its writer. |
| Cohort guard | A reference to a protected observation, directly or through a measurement, job, or artifact, gives 422 for an agent and writes a refused access event. A record that refers to a protected observation is hidden from the agents. |
| Budget | `GET /candidates/{id}` counts the observations of the candidate and of its resolved evidence as observation reads. |

The API does not redact free text. A person's note, reason, or summary in a blinded campaign can contain withheld values.

### Agent tasks

Agents coordinate through agent tasks. The API keeps no task state in memory.

1. A client with `record.write` writes a task in a campaign (`POST /tasks`). The task is `queued`. The inputs of a task must be visible to the agents of its campaign.
2. A client claims the next task (`POST /tasks/claim`). The claim takes the highest priority first, then the oldest task. It uses `SELECT … FOR UPDATE SKIP LOCKED`, so parallel claims never return the same task.
3. The claim returns `lease_id`. The holder extends the lease with `POST /tasks/{id}/heartbeat` before `lease_expires_at`.
4. The holder ends the task with an `agent-task-report/1` document (`POST /tasks/{id}/report`): `complete` or `failed`, with outputs. Or it returns the task to the queue (`POST /tasks/{id}/release`).

Only the client in `claimed_by` with the current `lease_id` can extend, release, or end a lease.
Another client gets 403. An earlier lease of the same client gets 409.
A claim also takes a claimed task whose lease expired, while attempts remain. That claim ends the earlier lease.
A lease that expired, but that no other claim took, can still be extended.
A task whose last lease expired becomes `failed` at the next claim in its campaign. The claim response lists it in `failed_on_expiry`.
`lease_expired` in a task response shows an expired lease before that claim.
A client cancels a queued or claimed task that it wrote. A client with `admin` cancels any task.

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
| `candidate-status/1` | status | The newest assessment on each axis gives the current status of a candidate. The database view `candidate_status` applies it. |

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
| `GET /campaigns`, `GET /campaigns/{id}` | Campaigns with budget state, clients, and record counts. An agent sees only its own campaign. |
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
| `POST /candidates` | Writes a `candidate/1` record (`record.write`). A person names the campaign with `campaign_id`. |
| `GET /candidates` | Candidates with their statuses, newest first. Filters: `campaign_id`, `kind`, `tier`, `evidence_status`, `identity_status`, `observation_id`, `limit`, `offset`. |
| `GET /candidates/{id}` | One candidate with its observations, resolved evidence, assessments, decisions, and notes. |
| `POST /assessments`, `GET /assessments`, `GET /assessments/{id}` | Writes an `assessment/1` record, and lists assessments, newest first. Filters: `candidate_id`, `campaign_id`, `axis`. |
| `POST /decisions`, `GET /decisions`, `GET /decisions/{id}` | Writes a `decision/1` record, and lists decisions, newest first. Filters: `candidate_id`, `campaign_id`, `action`. |
| `POST /notes`, `GET /notes`, `GET /notes/{id}` | Writes a `note/1` record, and lists notes, newest first. Filters: `campaign_id`, and `subject_kind` with `subject_id`. |
| `POST /tasks`, `GET /tasks`, `GET /tasks/{id}` | Writes an `agent-task/1` record, and lists tasks in claim order. Filters: `campaign_id`, `state`, `kind`. |
| `POST /tasks/claim` | Claims the next task of the campaign. Body: optional `kinds`. `task` is null when no task is available. |
| `POST /tasks/{id}/heartbeat`, `/release`, `/report` | Lease operations of the holder. Each body has `lease_id`. `/report` also has `report`, an `agent-task-report/1` document. |
| `POST /tasks/{id}/cancel` | Cancels a queued or claimed task, with a reason. |

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
`test_records.py` checks the record documents, the separation of measurements, judgments, and statuses, the decision freeze, campaign isolation, blinded responses, reference integrity, and permissions.
`test_tasks.py` checks claim order, parallel claims, and a claim next to a locked task.
It also checks lease expiry and recovery, lease ownership, release, cancellation, and the campaign lifecycle of tasks.
`test_migrations.py` applies migration 0004 to a database that holds records of the earlier migrations.
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
