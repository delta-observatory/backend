# Cross-component contracts

This directory holds the reference definitions for data that crosses a component boundary.
The API, workers, and database migrations use these definitions.
Version 1 covers the TESS vertical slice and the research records of agent work. DECam records need a later contract version.

## Runtime boundary

The API is stateless.
It keeps no job, run, campaign, or session state in process memory.
Any API replica can serve any request.

The API and workers do not call each other.
They exchange data only through PostgreSQL records and file storage references.
The API remains available when no worker runs.
Workers continue queued work when the API is unavailable.

The contracts in this directory are the only coupling between the API and workers.

## Contracts

| Contract | File | Producer | Consumer | Content |
|---|---|---|---|---|
| Job | `job/v1.schema.json` | API or worker | Workers | Contract version, run, method and method version, parameters, input references, and resource limits. |
| Result | `result/v1.schema.json` | Workers | API | Outcome, measurements with units and uncertainty, artifact references, diagnostics, resources, and software versions. |
| Method parameters | `methods/<method-id>/v1.schema.json` | This directory | API and workers | Parameter names, units, types, ranges, defaults, and scientific status for each method version. |
| Columnar artifact | `artifacts/columns/v1.schema.json` | Workers | API and frontend | Equal-length named arrays with units and time conventions. |
| Research records | `records/<record>/v1.schema.json` | API clients, through the API | API | Candidate, assessment, decision, note, agent task, and agent task report documents. `records/common/v1.schema.json` holds their shared references. |
| Export manifest | `exports/manifest/v1.schema.json` | API | API clients and export checks | `manifest.json` of an evidence export: subject, exporting client, contract source, withheld values, and the size and SHA-256 of each file. |
| Selection manifest | `inputs/tess-spoc-selection/v1.schema.json` | `datasets/` repository, after review | Workers | Light-curve files in subdirectories of `raw/<dataset_id>/` that the index method indexes, with owner, registration, size, SHA-256, and source identifier. |
| Records | `../migrations/` | Database migrations | API and workers | Tables, columns, constraints, and states. |

Method parameter schemas live here so that the API can validate requests without a running worker.
The backend owns the HTTP interface between the backend and frontend. That interface is not a contract in this directory.

## Methods

| Method | Kind | Scientific status |
|---|---|---|
| `tess-spoc-index` v1 | Index | Ingestion only. No scientific calculation. |
| `tess-bls` v1 | Analysis | Provisional. The defaults are the fixed T01 settings. Depth accuracy, completeness, and false-alarm rates are not validated. |

Each method schema carries an `x-method` object with its summary, scientific basis, and status.
Numeric parameters carry an `x-unit` annotation.
JSON Schema does not apply `default` values. The API fills defaults before it writes a job document.
`x-constraints` lists rules between fields that JSON Schema cannot express. The API and the worker both check them.

## Research records

| Contract | Schema | Content |
|---|---|---|
| `candidate/1` | `records/candidate/v1.schema.json` | Kind, tier, optional label, summary, observations, and evidence. No measured values and no status. |
| `assessment/1` | `records/assessment/v1.schema.json` | One status on the `evidence` or `identity` axis, with basis, method or decision, reason, evidence, and quality flags. |
| `decision/1` | `records/decision/v1.schema.json` | One advisory judgment: question, judge, model, packet checksum, policy, judged evidence status, action, reason, evidence, response, and scores. |
| `note/1` | `records/note/v1.schema.json` | Free text and its subjects. |
| `agent-task/1` | `records/agent-task/v1.schema.json` | Kind, title, instructions, inputs, priority, lease length, and attempts. |
| `agent-task-report/1` | `records/agent-task-report/v1.schema.json` | The end of a claimed task: outcome, summary, outputs, and the reason of a failure. |
| `investigation-plan/1` | `records/investigation-plan/v1.schema.json` | An immutable exploratory plan revision with exact inputs, method settings, exclusions, limits, and stop conditions. |
| `investigation-event/1` | `records/investigation-event/v1.schema.json` | An append-only product event with record references and a credential-free message. |

A record document holds the values that its writer supplies.
An import route can give a candidate, assessment, decision, or note an `import_key`. A second record with the same key is refused, so a repeated import writes nothing. Agents do not write import keys.
The API adds the writer, the time, and the campaign. The database adds the state of an agent task.
A reference names a platform record by kind and ID, or an external file by URI, SHA-256, and size.
An external file is a proposal, not platform evidence. A `platform` candidate needs a measurement or a job in its evidence.
A missing value has a reason: the model of a model judgment, the packet checksum, and the decision policy.

An investigation uses the existing campaign identity.
Plan revisions preserve their parents and exact document hashes.
A person approves an exact revision checksum before execution.
Execution links bind each run to its approved revision and a unique request key.
Repeated requests with that key refer to the same run.
Plan approval does not authorize independent scientific evaluation or remove campaign blinding and cohort restrictions.

The record documents keep four things apart:

| Item | Where it is |
|---|---|
| Measurements | `measurement` rows of worker methods. A record refers to them and does not copy them. |
| Model judgments | `decision/1` with judge `model`. Scores are judgments, not uncertainties or probabilities. |
| Evidence status | `assessment/1` on the `evidence` axis. |
| Identity status | `assessment/1` on the `identity` axis. A model judgment cannot give an identity status. |

The "Research records" section of the database repository README describes the tables, the triggers, and the task lease.

## Conventions

- A unit string of `"1"` marks a dimensionless value.
- A time value carries `time_scale` and `time_reference`. TESS times use `tdb` and `BJD 2457000.0`.
- A measurement has a standard uncertainty or an `unavailable` uncertainty with a reason.
- A `search` measurement comes from the blind method. A `reference_comparison` measurement compares the saved search output with a known object.
- Artifact paths are relative to the artifact storage root. A `gzip` artifact is stored compressed.

## Layout

```text
contracts/
├── README.md
├── job/v1.schema.json
├── result/v1.schema.json
├── methods/<method-id>/v1.schema.json
├── artifacts/columns/v1.schema.json
├── records/common/v1.schema.json
├── records/<record>/v1.schema.json
├── exports/manifest/v1.schema.json
├── inputs/tess-spoc-selection/v1.schema.json
└── examples/<job|result|methods|artifacts|records|exports|inputs>/<name>.<valid|invalid>.json
```

Schemas use JSON Schema, draft 2020-12.
The `tess-bls` job and result examples come from the closed T01 record for WASP-18 b.
The lab examples of the candidate and decision records use the C001 drafts of the delta-lab TESS pass.
Each schema has at least one valid example and one invalid example. `records/common/v1.schema.json` defines no record and has no examples.
A method, record, or input example name starts with its schema directory, for example `decision-lab-c001-triage.valid.json`.
Record schemas refer to `records/common/v1.schema.json` by `$id`. Consumers resolve the references with a registry of all schemas.
`scripts/check_contracts.py` checks every schema and example.

## Evidence exports

An export is a ZIP file with `manifest.json` (contract `export/1`) and the files that the manifest lists.
Each listed file has its size and SHA-256. The manifest holds no request time, so the same request on the same records gives the same bytes.
An artifact file in an export has the stored bytes. Its `stored_sha256` equals its `sha256` unless blinding redacted it.
The API README describes the export endpoints and the check script.

## Selected observations

The index method indexes the files in `raw/<dataset_id>/` and, in addition, the files that a selection manifest lists.
A selection manifest names files in subdirectories, for example the downloads of a research agent. The method does not scan subdirectories.
Each manifest names the owner of the files and the cohort register entry that registers them. Richard decides each registration.
The method checks the size and SHA-256 of each listed file. It records the manifest, owner, source identifier, and registration in `observation.provenance`.
A file with the content of an indexed observation does not become a second observation.

## Versions

Each job and result document carries its contract name and major version, for example `job/1`.
A compatible change adds an optional field.
A rename, removal, type change, unit change, or meaning change requires a new major version.
A new major version is a new file. Do not edit a released schema file.

Workers declare the job versions that they support.
A worker rejects an unsupported job version before execution and records the failure.
The API submits only job versions that the deployed release set supports.
The `kubernetes/` manifests select compatible API, worker, and database releases.

A contract version and the database migrations that it needs release together in one tagged release of this repository.

## Consumers

The API and workers pin a commit or release tag of this repository.
Each consumer copies the pinned `contracts/` directory into `vendor/contracts/` with `scripts/sync_contracts.sh`.
The copy records the source commit in `vendor/contracts/SOURCE`.
Consumers do not import code from sibling repositories.
Each consumer tests its output documents against the pinned schemas and examples.

## Rules for agents

Define job, result, method-parameter, and record shapes only in this repository.
Do not write these shapes by hand in `backend/` or `workers/`.
Change the contract here first. Then update each consumer.

## Bounded archive acquisition

`acquisition-request/1` selects one TIC target and at most eight TESS sectors.
The request sets file, byte, and free-space limits.
`acquisition-manifest/1` identifies exact public MAST products before approval.
`acquisition-result/1` records completed files, SHA-256 checksums, and failures.
The worker checks FITS checksums and compatibility before indexing an acquired file.
Only a person can approve the exact manifest checksum.
The worker preserves completed files when an incomplete transfer repeats.

## Managed agent sessions

`agent-session/2` supports OpenAI and Anthropic sessions. Version 1 remains available for existing OpenAI sessions.
The record contains provider identity, the approved revision, connection state, consent, and measured usage.
A session has a separate client with campaign scope.
The API reserves each turn against the approved investigation limits.
Usage notifications stop further tools and turns when the measured token limit is reached.
Provider notifications can arrive after token use. The limit is not a provider spending guarantee.
Credentials are not session fields and never enter these records.

## Desktop reproduction packages

`reproduction-package/1` wraps a checked evidence export and optional original observation files.
Each input retains its original SHA-256, size, and source identifier.
The desktop imports these files into a separate investigation through normal indexing and approval.
It preserves the original evidence without inserting copied measurement records into the database.
`reproduction-report/1` compares new measurements with the exported measurements.
The fixed tolerances measure numerical agreement. They do not represent scientific uncertainty.
The report records runtime agreement separately from numerical agreement.

## Desktop storage and recovery

`desktop-storage/1` sets the workspace size limit and free-space reserve.
`desktop-backup/1` lists checked CSV table data, files, and migration hashes.
A restore creates tables from trusted application migrations. It imports table values without running SQL from the backup.
The restore preserves the source workspace and regenerates local access credentials.
