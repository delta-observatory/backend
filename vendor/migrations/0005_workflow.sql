-- Workflow records: run retries, finished jobs, imported research records, and observation provenance.
--
-- A retry run repeats the failed and cancelled jobs of an earlier run. It uses the same method version and the
-- same parameters. Completed jobs of the earlier run stay, and the retry does not repeat them.
-- A job in a finished state does not change. A running job can become cancelled: the worker that holds it loses
-- the lease at its next extension and discards the output of the attempt.
-- An import key names a research record that an import route wrote. A second record with the same key is refused.
-- The provenance of an observation names the selection manifest, owner, and source identifier of a selected file.
--
-- Errors that these rules raise:
--   RC002  a finished job does not change
--   RC003  a retry run does not repeat the method version and parameters of its earlier run

ALTER TABLE run ADD COLUMN retry_of uuid REFERENCES run (id);
CREATE INDEX run_retry_idx ON run (retry_of) WHERE retry_of IS NOT NULL;

CREATE FUNCTION run_retry_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    earlier run%ROWTYPE;
BEGIN
    IF NEW.retry_of IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT * INTO earlier FROM run WHERE id = NEW.retry_of;
    IF earlier.method_id <> NEW.method_id OR earlier.method_version <> NEW.method_version
            OR earlier.parameters_sha256 <> NEW.parameters_sha256
            OR earlier.campaign_id IS DISTINCT FROM NEW.campaign_id THEN
        RAISE EXCEPTION 'run % does not repeat the method version, parameters, and campaign of run %',
            NEW.id, NEW.retry_of USING ERRCODE = 'RC003';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER run_retry_check BEFORE INSERT ON run
    FOR EACH ROW EXECUTE FUNCTION run_retry_check();

CREATE FUNCTION job_finished() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state IN ('complete', 'inconclusive', 'failed', 'cancelled') THEN
        RAISE EXCEPTION 'job % is %. A finished job does not change.', OLD.id, OLD.state USING ERRCODE = 'RC002';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER job_finished BEFORE UPDATE ON job
    FOR EACH ROW EXECUTE FUNCTION job_finished();

ALTER TABLE candidate ADD COLUMN import_key text CHECK (import_key ~ '^[a-z0-9][a-z0-9._:/-]{0,199}$');
ALTER TABLE assessment ADD COLUMN import_key text CHECK (import_key ~ '^[a-z0-9][a-z0-9._:/-]{0,199}$');
ALTER TABLE decision ADD COLUMN import_key text CHECK (import_key ~ '^[a-z0-9][a-z0-9._:/-]{0,199}$');
ALTER TABLE note ADD COLUMN import_key text CHECK (import_key ~ '^[a-z0-9][a-z0-9._:/-]{0,199}$');
CREATE UNIQUE INDEX candidate_import_key_idx ON candidate (import_key) WHERE import_key IS NOT NULL;
CREATE UNIQUE INDEX assessment_import_key_idx ON assessment (import_key) WHERE import_key IS NOT NULL;
CREATE UNIQUE INDEX decision_import_key_idx ON decision (import_key) WHERE import_key IS NOT NULL;
CREATE UNIQUE INDEX note_import_key_idx ON note (import_key) WHERE import_key IS NOT NULL;

-- Empty for a file in the dataset directory. For a file of a selection manifest (format tess-spoc-selection/1):
-- selection (the manifest path), owner, source_id, and registration (the cohort register entry).
ALTER TABLE observation ADD COLUMN provenance jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(provenance) = 'object');
