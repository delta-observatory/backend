-- Records for the TESS vertical slice: datasets, observations, runs, jobs,
-- measurements, and artifacts. contracts/ defines the JSON documents that
-- job.document and job.result hold.

CREATE TABLE dataset (
    id              text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9-]*$'),
    state           text NOT NULL DEFAULT 'unavailable'
                    CHECK (state IN ('unavailable', 'indexing', 'ready', 'incomplete', 'invalid')),
    findings        jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(findings) = 'array'),
    last_indexed_at timestamptz
);

-- One row for each distinct file content. A changed file at the same path
-- gets a new row, and the earlier row becomes 'missing'.
CREATE TABLE observation (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id       text NOT NULL REFERENCES dataset (id),
    product_type     text NOT NULL CHECK (product_type IN ('tess_spoc_light_curve')),
    relative_path    text NOT NULL CHECK (relative_path !~ '(^/|\.\.)'),
    bytes            bigint NOT NULL CHECK (bytes > 0),
    sha256           text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    target_id        text NOT NULL CHECK (target_id ~ '^TIC:[0-9]+$'),
    sector           integer CHECK (sector > 0),
    cadence_seconds  double precision CHECK (cadence_seconds > 0),
    time_start       double precision,
    time_stop        double precision CHECK (time_stop >= time_start),
    time_scale       text CHECK (time_scale IN ('tdb', 'utc', 'tt')),
    time_reference   text,
    row_count        integer CHECK (row_count >= 0),
    header           jsonb NOT NULL DEFAULT '{}'::jsonb,
    labels           jsonb NOT NULL DEFAULT '{}'::jsonb,
    state            text NOT NULL CHECK (state IN ('ready', 'invalid', 'missing')),
    findings         jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(findings) = 'array'),
    first_indexed_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (dataset_id, sha256)
);
CREATE INDEX observation_target_idx ON observation (target_id);
CREATE UNIQUE INDEX observation_present_path_idx ON observation (dataset_id, relative_path)
    WHERE state <> 'missing';

-- A run is an immutable method version, parameter set, and input selection.
CREATE TABLE run (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    method_id         text NOT NULL,
    method_version    integer NOT NULL CHECK (method_version >= 1),
    parameters        jsonb NOT NULL CHECK (jsonb_typeof(parameters) = 'object'),
    parameters_sha256 text NOT NULL CHECK (parameters_sha256 ~ '^[0-9a-f]{64}$'),
    label             text,
    requested_by      text NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX run_created_idx ON run (created_at DESC);

CREATE TABLE run_input (
    run_id         uuid NOT NULL REFERENCES run (id),
    observation_id uuid NOT NULL REFERENCES observation (id),
    PRIMARY KEY (run_id, observation_id)
);

-- The job table is also the work queue. Workers claim rows with
-- SELECT ... FOR UPDATE SKIP LOCKED and hold a lease while they execute.
CREATE TABLE job (
    id               uuid PRIMARY KEY,
    run_id           uuid NOT NULL REFERENCES run (id),
    observation_id   uuid REFERENCES observation (id),
    document         jsonb NOT NULL CHECK (document ->> 'contract' = 'job/1'),
    state            text NOT NULL DEFAULT 'queued'
                     CHECK (state IN ('queued', 'running', 'complete', 'inconclusive', 'failed', 'cancelled')),
    attempts         integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts     integer NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    available_at     timestamptz NOT NULL DEFAULT now(),
    claimed_by       text,
    lease_expires_at timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz,
    result           jsonb CHECK (result IS NULL OR result ->> 'contract' = 'result/1'),
    CHECK ((document ->> 'job_id')::uuid = id),
    CHECK ((document ->> 'run_id')::uuid = run_id),
    CHECK (state <> 'running' OR lease_expires_at IS NOT NULL),
    CHECK (state NOT IN ('complete', 'inconclusive', 'failed') OR (result IS NOT NULL AND finished_at IS NOT NULL))
);
-- One job for each observation in a run, and one job for a run with no input.
CREATE UNIQUE INDEX job_run_observation_idx ON job (run_id, observation_id) NULLS NOT DISTINCT;
CREATE INDEX job_claim_idx ON job (available_at) WHERE state = 'queued';
CREATE INDEX job_lease_idx ON job (lease_expires_at) WHERE state = 'running';
-- At most one index job for a dataset waits or runs at a time.
CREATE UNIQUE INDEX job_one_active_index_idx ON job ((document -> 'parameters' ->> 'dataset_id'))
    WHERE document -> 'method' ->> 'id' = 'tess-spoc-index' AND state IN ('queued', 'running');

CREATE FUNCTION job_notify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('delta_jobs', NEW.id::text);
    RETURN NEW;
END;
$$;
CREATE TRIGGER job_notify_insert AFTER INSERT ON job
    FOR EACH ROW WHEN (NEW.state = 'queued') EXECUTE FUNCTION job_notify();

CREATE TABLE measurement (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id             uuid NOT NULL REFERENCES job (id),
    observation_id     uuid NOT NULL REFERENCES observation (id),
    role               text NOT NULL CHECK (role IN ('search', 'reference_comparison')),
    name               text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]*$'),
    value_number       double precision,
    value_boolean      boolean,
    unit               text NOT NULL CHECK (unit <> ''),
    time_scale         text CHECK (time_scale IN ('tdb', 'utc', 'tt')),
    time_reference     text,
    uncertainty_kind   text NOT NULL CHECK (uncertainty_kind IN ('standard', 'unavailable')),
    uncertainty_value  double precision CHECK (uncertainty_value >= 0),
    uncertainty_reason text,
    quality_flags      text[] NOT NULL DEFAULT '{}',
    UNIQUE (job_id, observation_id, role, name),
    CHECK (value_number IS NULL OR value_boolean IS NULL),
    CHECK ((time_scale IS NULL) = (time_reference IS NULL)),
    CHECK ((uncertainty_kind = 'standard' AND uncertainty_value IS NOT NULL AND uncertainty_reason IS NULL)
        OR (uncertainty_kind = 'unavailable' AND uncertainty_value IS NULL AND uncertainty_reason <> ''))
);
CREATE INDEX measurement_observation_idx ON measurement (observation_id, name);

CREATE TABLE artifact (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id           uuid NOT NULL REFERENCES job (id),
    observation_id   uuid REFERENCES observation (id),
    kind             text NOT NULL
                     CHECK (kind IN ('light_curve', 'prepared_series', 'periodogram', 'search_summary', 'input_checks')),
    media_type       text NOT NULL CHECK (media_type IN ('application/json', 'application/vnd.delta.columns+json')),
    content_encoding text NOT NULL CHECK (content_encoding IN ('identity', 'gzip')),
    path             text NOT NULL UNIQUE CHECK (path ~ '^[A-Za-z0-9][A-Za-z0-9._/-]*$' AND path !~ '\.\.'),
    bytes            bigint NOT NULL CHECK (bytes >= 0),
    sha256           text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX artifact_observation_idx ON artifact (observation_id, kind);
CREATE INDEX artifact_job_idx ON artifact (job_id);
