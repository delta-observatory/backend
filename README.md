# Delta Observatory API

This repository is one component of [Delta Observatory](https://github.com/delta-observatory/delta-observatory). The entry repository describes the application and restores every component.

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
| `DELTA_DATASETS_ROOT` | `/data/datasets` | Read-only dataset catalog repository. |
| `DELTA_EXPERIMENTS_ROOT` | `/data/experiments` | Read-only experiment index and registered descriptions. |
| `DELTA_CORS_ORIGINS` | empty | Comma-separated origins that can call the API from a browser. |
| `DELTA_COHORTS_PATH` | `/data/experiments/cohorts.json` | The shared cohort register, mounted read-only. The cohort guard reads it. |

The API keeps no job, run, or session state in process memory.
Any replica can serve any request.
Pool connections use autocommit. Each write transaction commits before the response, so a read just after a write sees it.

## Authentication and permissions

Under `/api/`, every endpoint except `GET /health` and the API description needs a bearer token: `Authorization: Bearer <token>`.
A missing, unknown, or disabled token gives 401. A missing permission gives 403.
Each token belongs to one client in the `client` table: a person, an agent, or a service. The API stores only the SHA-256 of a token.

| Permission | Allows |
|---|---|
| `read` | Every read endpoint, and `POST /runs/estimate`. |
| `run.submit` | `POST /runs`, `POST /runs/{id}/retry`, and the cancellation of the client's own runs and jobs. |
| `record.write` | Research-record writes: candidates, assessments, decisions, notes, and agent tasks. `POST /campaigns/{id}/fix-decisions` for an agent of the campaign. |
| `acquire` | Acquisition of new observations. No endpoint uses it yet. |
| `identity.reveal` | `POST /campaigns/{id}/reveal`. |
| `admin` | Client and campaign management, access events, dataset indexing, and cancellation of any run or job. |

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

### Agent scope by endpoint

An agent sees the runs of its campaign and the runs without a campaign, for example Richard's runs and the index runs.
A run of another campaign is "run not found" (404) on every path that reaches it.
These paths include its jobs, its artifacts, `GET /targets?run_id=`, comparisons, exports, retries, and cancellations.
A reference to a run, job, measurement, or artifact of another campaign in a record gives "not found" (422).

| Endpoints | Before the approval and after the close | Inside an approved, open campaign |
|---|---|---|
| `GET /me`, `GET /methods`, `GET /rules`, `GET /campaigns`, `GET /campaigns/{id}` | Allowed. The agent sees only its campaign. | Allowed. |
| `GET /datasets`, `/observations`, `/targets` | 403 | Allowed. The API leaves out protected observations. Without identity, a dataset or observation finding keeps only its kind and its counts, for example `path_count`. |
| `GET /runs`, `/runs/default`, `/runs/{id}`, `/runs/compare` | 403 | Visible runs only. Inputs, jobs, and job counts leave out protected observations, with the number in `protected_excluded`. `default-run/1` chooses among the visible runs. |
| `GET /runs/{id}/results`, `/runs/{id}/observations/{oid}/fold`, `/jobs/{id}`, `/artifacts/{id}/content` | 403 | Visible runs only. Each observation whose data the response holds counts toward the observation budget, also through the light-curve artifacts of an index run. The results leave out the jobs and artifacts of protected observations. A direct read of a protected observation gives 403 and a refused access event. Without identity, a job error keeps its `kind` and loses `message` and `traceback`. |
| `GET /artifacts/{id}` | 403 | Visible runs only. Metadata does not count toward the observation budget. An artifact of a protected observation gives 403 and a refused access event. |
| `POST /runs/estimate`, `POST /runs`, `POST /runs/{id}/retry` | 403 | A protected observation gives 422 and a refused access event. Budgets apply. After `fix-decisions`, `POST /runs` and retries give 403. |
| `POST /runs/{id}/cancel`, `POST /jobs/{id}/cancel` | 403 | The agent's own runs only. |
| `GET /runs/{id}/export`, `GET /candidates/{id}/export` | 403 | The same scope, blinding, cohort guard, and budget as the views. |
| Research records | 403 | The records of the campaign. The next sections give the rules. |

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
The budget check of a run submission and the insert hold a lock on the campaign, and they read the campaign state again inside it.
Two parallel submissions therefore cannot both pass a budget.
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

An import route can give a candidate, assessment, decision, or note an `import_key`, for example `research:<agent>/<study>/<item>`.
A second record with the same key gives 409 with `existing_id` in the detail, so a repeated import writes nothing.
An agent with an import key gets 403.
An imported proposal uses tier `lab` and external references. A judgment from an external source is a decision with judge `model`. It is not a measurement.

The status of a candidate follows rule `candidate-status/1`: the newest assessment on each axis gives it.
Without an assessment, the evidence status is `unreviewed` and the identity status is `unchecked`.
Each candidate response gives `evidence_status` and `identity_status` with the assessment and its basis.
A status is null when its source assessment is hidden from the client.
Status filters exclude that candidate on the affected axis. List totals follow the same rule.
The API does not substitute an earlier assessment for a hidden current assessment.

Agent reads of research records require an approved campaign that is not closed.
The API returns 403 for record lists and details outside that period.

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

### Free text

Rule `free-text/1` controls free text while a campaign withholds labels or identity from its agents, before the reveal.

| Text | What an agent of the campaign receives |
|---|---|
| Text from a client of the agent's own campaign | The text. |
| Directed text: the campaign title and question, and the title, instructions, and inputs of an agent task | The text. The API does not check it. Its writer is responsible for its content. |
| Other writers' text. This includes run labels, candidate summaries and labels, assessment reasons, decision reasons and responses, and note bodies. It also includes the model, packet, and policy reasons and the model provider and ID of a decision. Task report summaries and reasons, import keys, and the URI and description of external references are other writers' text too. | Null. The record has `text_withheld: true`. A list or detail response adds `text` to `withheld`. |

Persons and services receive all text.
The API does not search text for withheld values. A writer can put a withheld value in directed text, and an agent can learn a value from another source.
This handling therefore does not guarantee scientific blinding. Richard keeps withheld values out of directed text.

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
Agent claims exclude hidden tasks from selection and expiry processing. Those tasks retain their state and attempt count.
The API prepares the claim response before it commits the claim, using the same visibility rules as task reads.
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
| `run-comparison/1` | display | Stored measurements of two runs of one method, side by side. The difference b − a is a display calculation without uncertainty, because both values come from the same observations. |
| `free-text/1` | access | The free text that an agent receives while blinding withholds values. |

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
| `GET /observations/{id}` | One observation with header, labels, findings, provenance, and light-curve artifacts. `provenance` names the selection manifest, owner, source identifier, and registration of a selected file. It is empty for a file in the dataset directory. |
| `POST /runs` | Creates a run and one queued job for each observation. The status code is 201. |
| `POST /runs/estimate` | Checks a run request as `POST /runs` does and writes nothing. Returns the full parameters and the work estimate. The estimate has the trial periods of each observation (rule `bls-grid/1`) and the trial limit. It also has the worker time, from the median rate of the newest 200 complete jobs of the method. A failed check gives the same 422 as `POST /runs`. |
| `GET /runs` | Runs, newest first, with job counts by state and `retry_of`. `observation_id` limits the list to runs with a job for that observation. An agent gets no runs for a protected observation. |
| `GET /runs/default` | The default run (rule `default-run/1`). Filters: `dataset_id`, `method_id`, `method_version`. 404 when no analysis run has a complete job. |
| `GET /runs/compare` | Two runs of one method side by side (rule `run-comparison/1`): `run_a`, `run_b`, optional `observation_id`, `limit`, `offset`. Gives `parameter_differences` and counts of observations in both runs, in one run only, and with a different job state. Each observation row gives each run's job, state, measurements, and the display differences. Different methods give 422. |
| `GET /runs/{id}` | One run with parameters, inputs, jobs, `retry_of`, and `retries`. |
| `POST /runs/{id}/cancel` | Cancels every queued and running job of the client's own run, or of any run with `admin`. Gives the number of cancelled jobs and of jobs that were running. |
| `POST /runs/{id}/retry` | A new run for the failed and cancelled jobs of the run. Body: optional `label`. The status code is 201. |
| `GET /runs/{id}/export` | An evidence export of the run as a ZIP file. `artifacts=none` (default) or `all`. |
| `GET /runs/{id}/results` | Measurements by role, artifacts, diagnostics, resources, and software for each job. `observation_id` limits the response to one job. |
| `GET /runs/{id}/observations/{observation_id}/fold` | The stored flattened flux folded and binned (rule `fold-window/1`). Defaults: the stored BLS period and transit time. Options: `period`, `t0`, `window_hours`, `bin_hours`, `t_min`, `t_max`, and `points`. The box model shows only for the stored ephemeris. |
| `GET /jobs/{id}` | One job with its job and result documents. |
| `POST /jobs/{id}/cancel` | Cancels a queued or running job of the client's own run, or of any run with `admin`. The response gives `was_running`. A finished job gives 409. |
| `GET /artifacts/{id}` | Artifact metadata. |
| `GET /artifacts/{id}/content` | The stored file. A `gzip` artifact has `Content-Encoding: gzip`. The ETag is the SHA-256. |
| `POST /candidates` | Writes a `candidate/1` record (`record.write`). A person names the campaign with `campaign_id`. |
| `GET /candidates` | Candidates with their statuses, newest first. Filters: `campaign_id`, `kind`, `tier`, `evidence_status`, `identity_status`, `observation_id`, `limit`, `offset`. |
| `GET /candidates/{id}` | One candidate with its observations, resolved evidence, assessments, decisions, and notes. |
| `GET /candidates/{id}/export` | An evidence export of the candidate as a ZIP file. `artifacts=none` (default) or `all`. |
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

### Cancellation, failures, and retries

The API cancels a queued job at once. A running job also becomes `cancelled` at once, and the worker stops at a safe boundary:
1. The worker that holds the job extends its lease every third of the lease length. The deployed lease is 60 s, so this is every 20 s.
2. The extension fails, because the job is no longer `running`. The worker stops the method process.
3. The worker discards the output. Its final write also requires the `running` state, so no measurement or artifact row appears.

The files of that attempt stay under `jobs/<job_id>/<attempt>/` without artifact rows. The worker time of a cancelled job is not recorded, so the campaign compute budget does not count it.
A running index job is not cancelled. An index job stops only at its end.
A finished job does not change. A database trigger refuses the update.

A failed job keeps its error `kind` and `message`: `contract`, `missing_input`, `time_limit`, `memory_limit`, `lease_lost`, `lease_expired`, `child_exit`, or the exception name of the method.
The worker does not repeat a failed job. It repeats only a job whose lease expired, up to `max_attempts`, and each attempt starts again: a method does not resume partial work.
A worker restart returns its running job to the queue.
`POST /runs/{id}/retry` repeats the failed and cancelled jobs in a new run with `retry_of`, the same method version, the same parameters, and the same campaign. The database checks this.
A retry leaves out an observation that an earlier retry of the same run holds in a job that is not failed or cancelled.
Retries of one run hold a lock on the run, so two parallel retries cannot queue the same observation twice. When no observation remains, the retry gives 409.
The complete and inconclusive jobs of the earlier run stay, and the retry does not repeat them. Measurements are therefore not duplicated.
An inconclusive job is a scientific outcome, not a technical failure, so a retry does not repeat it.
The campaign of a retry must be the campaign of the run.
A person therefore cannot retry an agent's run, and an agent cannot retry a run without a campaign.

### Evidence exports

`GET /runs/{id}/export` and `GET /candidates/{id}/export` return a ZIP file with `manifest.json` (contract `export/1` in `vendor/contracts/exports/manifest/`).
The manifest lists each other file with its size, SHA-256, media type, and content.
The header `X-Export-Manifest-SHA256` gives the SHA-256 of the manifest.

| File | Content |
|---|---|
| `README.txt` | The subject, the exporting client, the contract source, the files, and the limitations. |
| `run.json` | The run detail, the method description and parameter schema, and each job with its job document, result document, and software versions. |
| `runs.json` | The candidate export: each referenced run and its referenced jobs. |
| `candidate.json` | The candidate export: the candidate detail with resolved evidence, assessments, decisions, notes, and actors. |
| `records.json` | The run export: the candidates that refer to the run, its jobs, measurements, or artifacts, each with its detail. |
| `observations.json` | Observation identifiers, file SHA-256, time conventions, header values, and provenance. |
| `measurements.json`, `measurements.csv` | Measurement ID, job, observation, target, role, name, value, unit, uncertainty, time convention, and quality flags. |
| `artifacts.json` | Artifact identifiers, kinds, sizes, and SHA-256, including the light curves of the observations. |
| `methods.json` | Method descriptions and scientific status. |
| `artifacts/<artifact_id>/<file>` | With `artifacts=all`: the stored artifact bytes. Their `sha256` equals `stored_sha256`. A file that blinding redacted has `redacted: true`. |

The ZIP entries have a fixed date and fixed attributes, and the JSON has sorted keys. The manifest holds no request time.
`observations.json` leaves out the index times, so a repeated index does not change an export.
The same request on the same stored records therefore gives the same bytes. A new record that refers to the run changes the export.
An export holds the view of the exporting client. Blinding, the free-text rule, the cohort guard, and the observation budget apply.
For an agent, an export leaves out the artifacts of protected observations. It counts each observation of an included artifact as a read before it adds the bytes.
The API checks refusals before it records reads where it can. A budget refusal during the records part of an export can follow recorded reads.
The export lists external files in the evidence by URI, SHA-256, and size. It does not include them.
The API builds the ZIP file in a temporary file. Up to 32 MiB stay in memory, and a larger export uses `/tmp`.

Check an export with the script. It needs only the Python standard library. `--schema` also checks the manifest with `jsonschema`.
`--manifest-sha256` compares the manifest with the `X-Export-Manifest-SHA256` header of the response:

```bash
python scripts/verify_export.py export.zip --schema vendor/contracts/exports/manifest/v1.schema.json --manifest-sha256 <hex>
```

The script checks the size and SHA-256 of each listed file. It also finds files that the manifest does not list and unsafe paths, including drive paths such as `C:/x`.
It reports a redacted file in an export that withholds nothing. An unreadable file or manifest gives a JSON failure report.
It reports an artifact file whose SHA-256 differs from the stored SHA-256 without the redacted mark.
It prints a JSON report. The exit status is 1 when a check fails.

### Measurements

Each measurement has `id`, `role`, `name`, `value`, `unit`, `uncertainty`, and `quality_flags`. A record refers to a measurement by its `id`.
`uncertainty` is `{"kind": "standard", "value": …}` or `{"kind": "unavailable", "reason": …}`.
A time value also has `time_scale` and `time_reference`.

## Catalog documents

`GET /api/catalog` reads dataset descriptions and registered experiment references from read-only repository mounts.
`catalog.json` registers datasets. The experiment index table registers experiments, and each `experiment.json` supplies its dataset roles and method scopes.
The API preserves the index status and result text. It does not infer scientific validity from closure or execution status.
Each source reports availability, issues, and a SHA-256. Metadata and manifests also have individual hashes.
Missing or invalid entries produce explicit issues. The API does not infer entries from directory names or scan observation files.

`GET /api/catalog/documents/{collection}/{identifier}/README.md` returns plain text, byte count, and SHA-256.
`collection` accepts only `datasets` and `experiments`. The entry must exist in its available catalog.
The API confines paths to the configured root and limits each source document to two million bytes.
These views describe documents. They do not create scientific records or change the database contracts.

Both catalog endpoints require `read` and a person or service client.
Agent clients receive 403 because repository text can disclose identities outside their campaigns.
Agents can use the existing campaign-scoped observation and result endpoints.

`GET /api/runs?dataset_id=<id>` selects runs with visible input observations from the dataset.
Campaign restrictions and protected-observation exclusions apply before pagination.
The returned job counts describe the whole visible run. Index jobs without observation inputs are excluded.

## Optional GUI browsing

`DELTA_GUI_READ_CLIENT` enables a separate `/gui-api/` channel for anonymous browsing.
The value names an enabled service client with exactly `read` permission and no campaign.
Create that client through the existing administrator workflow. The browser does not receive its token.
The setting is empty by default. Without it, `/gui-api/` returns 404 and the frontend retains its sign-in screen.

The GUI channel permits GET, HEAD, and the read-only `POST /runs/estimate` operation.
It rejects other methods with 403 before endpoint execution, including requests that carry a writer's token.
The existing permission checks also protect administrator reads.
Each anonymous request checks the configured client's identity, permissions, and disabled state in PostgreSQL.
A missing, disabled, or incorrectly privileged service produces 503.

An explicit Authorization header always uses normal token authentication. Invalid credentials never receive anonymous access.
An authenticated agent retains its campaign scope and blinding through either path.
The `/api/` channel still requires authentication. Anonymous browsing does not grant search, review, acquisition, reveal, or administration permissions.
`GET /gui-api/me` reports `anonymous: true` for the configured reader. Authenticated requests report `anonymous: false`.
The frontend proxy forwards both paths without inserting credentials.

## Signal review queue

`GET /api/signals` provides the `signal-review/1` view for exploration.
Each ready observation contributes its latest completed visible `tess-bls` job with a complete result.
A newer failed, cancelled, or incomplete job does not replace that evidence. `run_id` selects a particular run instead.
The view returns stored search measurements, run and job identifiers, and visible candidate links.
It introduces no detection threshold, probability, assessment, or new scientific record shape.

Filters accept `dataset_id`, `run_id`, target text `q`, and `sample` values `all`, `unconfirmed`, or `candidates`.
`unconfirmed` excludes labels `planet_host` and `eclipsing_binary`. Missing labels do not establish an unknown object.
`candidates` requires a visible candidate record for the observation.
`sort` accepts `snr`, `period`, `depth`, or `recent`. The default orders stored depth S/N from highest to lowest.
`limit` defaults to 100 and cannot exceed 200. `offset` provides pagination.

Campaign run scope, protected-observation exclusions, identity and label blinding, free-text restrictions, and read budgets apply.
The API rejects label-dependent or identity-dependent filters when the campaign withholds those fields.
The result gives selection and ranking explanations, measurement units, the total, and pagination fields.

## Tests

The tests use a temporary PostgreSQL 16 server from the `pgserver` package.
They apply `vendor/migrations/` to it. They do not use mocks for the database.
`test_signals.py` checks completed-result selection, stored ranking, filters, candidate visibility, campaign restrictions, protected observations, and read budgets.
`test_catalogue.py` checks catalog registration, exact source wording, missing sources, hashes, path confinement, permissions, and dataset filtering of runs.
`test_gui.py` checks optional anonymous reads, mutation refusal, invalid credentials, service revocation, and preserved agent blinding.
`test_auth.py` checks tokens, permissions, the agent restriction, and actor records.
`test_campaigns.py` checks that blinding, the cohort guard, budgets, and the lifecycle stop prohibited agent actions.
`test_records.py` checks the record documents, the separation of measurements, judgments, and statuses, the decision freeze, campaign isolation, blinded responses, reference integrity, and permissions.
`test_tasks.py` checks claim order, parallel claims, and a claim next to a locked task.
It also checks lease expiry and recovery, lease ownership, release, cancellation, and the campaign lifecycle of tasks.
`test_migrations.py` applies migrations 0004 and 0005 to a database that holds records of the earlier migrations.
`test_workflow.py` checks run scope for agents, the lifecycle of agent reads, and the protected observations in run views.
It checks identity redaction of paths, errors, and provenance, and the free-text rule.
It also checks running-job cancellation, retries, run comparison, reproducible exports and the check script, and import keys.
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

## License

The code is under the [MIT License](LICENSE).

## Investigation control

Investigations use existing campaign identities and worker jobs.
`GET /investigations/schema` supplies the canonical plan schema and supported method settings.
`POST /investigations` creates a draft with exact observation IDs and resource limits.
`POST /investigations/{id}/revisions` saves an immutable proposal and its parent revision.
The request includes the expected current revision, which prevents lost edits.
`POST /investigations/{id}/approve` requires the owner and the exact current revision checksum.
`POST /investigations/{id}/execute` submits the approved settings with an idempotent request key.
Repeated requests with the same key return the same run.
Pause blocks new submissions. Active jobs can finish.
Stop also cancels queued and running jobs.
Resume requires approval of the current revision.
The event endpoint preserves instructions, revisions, approvals, and execution links.

Investigation agents can read only approved, included observations and this campaign's runs.
They cannot approve proposals, expand the selection, or use generic submission and retry endpoints.
The existing cohort guard and blinding rules still apply.
An approved investigation keeps its blinding level.
A different blinding level requires a separate investigation.

## Archive acquisition

`POST /acquisitions` queues a bounded MAST metadata query for a person with `acquire` permission.
The worker writes the exact product manifest and its checksum.
`POST /acquisitions/{id}/approve` approves that checksum before downloading.
Cancellation stops transfers at the next chunk boundary.
Retry preserves checked files and repeats incomplete transfers.
The API never contacts MAST or the worker process.
