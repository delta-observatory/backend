# Cross-component contracts

This directory holds the reference definitions for data that crosses a component boundary.
The API, workers, and database migrations use these definitions.
Version 1 covers the TESS vertical slice. DECam records need a later contract version.

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
└── examples/<job|result|methods|artifacts>/<name>.<valid|invalid>.json
```

Schemas use JSON Schema, draft 2020-12.
The `tess-bls` job and result examples come from the closed T01 record for WASP-18 b.
Each schema has at least one valid example and one invalid example.
`scripts/check_contracts.py` checks every schema and example.

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
