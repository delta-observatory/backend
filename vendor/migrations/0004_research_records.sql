-- Research records (ROADMAP R8): candidates, assessments, decisions, notes, agent tasks, and their links.
-- contracts/records/ defines the documents that clients and workers write.
--
-- Candidates, assessments, decisions, notes, and links do not change after the insert. A correction is a new record.
-- A candidate holds no measured values and no status. Its values are the measurements that it refers to.
-- Its evidence status and identity status come from assessments (view candidate_status, rule candidate-status/1).
-- A decision holds a judgment. It does not set a status.
--
-- Errors that these rules raise:
--   RC001  the campaign state refuses the write (not approved, decisions fixed, or closed)
--   RC002  the record does not change after the insert
--   RC003  a reference or a link is not consistent with its record

CREATE TABLE candidate (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- A candidate without a campaign belongs to no agent. Agents see only the records of their campaign.
    campaign_id uuid REFERENCES campaign (id),
    label       text CHECK (label ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
    kind        text NOT NULL CHECK (kind IN ('transit_signal', 'moving_object')),
    -- platform: evidence from versioned worker methods. lab: evidence from agent code outside the platform.
    tier        text NOT NULL CHECK (tier IN ('platform', 'lab')),
    summary     text NOT NULL CHECK (summary <> ''),
    created_by  uuid NOT NULL REFERENCES client (id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, label)
);
CREATE INDEX candidate_campaign_idx ON candidate (campaign_id, created_at);

CREATE TABLE candidate_observation (
    candidate_id   uuid NOT NULL REFERENCES candidate (id),
    observation_id uuid NOT NULL REFERENCES observation (id),
    PRIMARY KEY (candidate_id, observation_id)
);
CREATE INDEX candidate_observation_idx ON candidate_observation (observation_id);

-- One judgment: question, supplied evidence and its checksum, judge, response, policy, and recommended action.
CREATE TABLE decision (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id           uuid NOT NULL REFERENCES candidate (id),
    assessment_id          uuid,
    question_id            text NOT NULL CHECK (question_id ~ '^[a-z][a-z0-9-]{0,62}$'),
    question_version       integer NOT NULL CHECK (question_version >= 1),
    -- advisory: a recommendation only. Automatic modes need R6 evaluation.
    mode                   text NOT NULL CHECK (mode IN ('advisory')),
    judge                  text NOT NULL CHECK (judge IN ('model', 'person')),
    model_provider         text CHECK (model_provider <> ''),
    model_id               text CHECK (model_id <> ''),
    model_reason           text CHECK (model_reason <> ''),
    packet_sha256          text CHECK (packet_sha256 ~ '^[0-9a-f]{64}$'),
    packet_reason          text CHECK (packet_reason <> ''),
    policy_id              text CHECK (policy_id ~ '^[a-z][a-z0-9-]{0,62}$'),
    policy_version         integer CHECK (policy_version >= 1),
    policy_reason          text CHECK (policy_reason <> ''),
    -- The status that the judge gives. It is part of the judgment, not the status of the candidate.
    judged_evidence_status text NOT NULL CHECK (judged_evidence_status IN ('supported', 'ambiguous', 'rejected')),
    action                 text NOT NULL CHECK (action IN ('discard', 'request_more_data', 'request_pixel_check',
                                                           'request_vetting', 'request_human_review', 'no_action')),
    reason                 text NOT NULL CHECK (reason <> ''),
    response               jsonb CHECK (response IS NULL OR jsonb_typeof(response) = 'object'),
    -- Numbers that the judge returned. Model judgments, not uncertainties or probabilities.
    scores                 jsonb CHECK (scores IS NULL OR jsonb_typeof(scores) = 'object'),
    -- The values that blinding withheld from the writer at the time of the write.
    withheld               text[] NOT NULL DEFAULT '{}' CHECK (withheld <@ ARRAY['labels', 'identity']::text[]),
    supersedes             uuid REFERENCES decision (id),
    created_by             uuid NOT NULL REFERENCES client (id),
    created_at             timestamptz NOT NULL DEFAULT now(),
    -- A model judgment names its model, or gives the reason that the model is unknown.
    CHECK ((judge = 'person' AND model_provider IS NULL AND model_id IS NULL AND model_reason IS NULL)
        OR (judge = 'model' AND model_provider IS NOT NULL AND model_id IS NOT NULL AND model_reason IS NULL)
        OR (judge = 'model' AND model_provider IS NULL AND model_id IS NULL AND model_reason IS NOT NULL)),
    CHECK ((packet_sha256 IS NULL) <> (packet_reason IS NULL)),
    CHECK ((policy_id IS NOT NULL AND policy_version IS NOT NULL AND policy_reason IS NULL)
        OR (policy_id IS NULL AND policy_version IS NULL AND policy_reason IS NOT NULL))
);
CREATE INDEX decision_candidate_idx ON decision (candidate_id, created_at);

-- One status of a candidate on one axis. The axes stay separate.
CREATE TABLE assessment (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Insert order. The newest assessment on an axis gives the current status.
    seq            bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    candidate_id   uuid NOT NULL REFERENCES candidate (id),
    axis           text NOT NULL CHECK (axis IN ('evidence', 'identity')),
    status         text NOT NULL,
    -- method: a versioned worker method. review: a person or an agent. model_judgment: the decision in decision_id.
    basis          text NOT NULL CHECK (basis IN ('method', 'review', 'model_judgment')),
    method_id      text CHECK (method_id ~ '^[a-z][a-z0-9-]{0,62}$'),
    method_version integer CHECK (method_version >= 1),
    decision_id    uuid REFERENCES decision (id),
    reason         text NOT NULL CHECK (reason <> ''),
    quality_flags  text[] NOT NULL DEFAULT '{}',
    supersedes     uuid REFERENCES assessment (id),
    created_by     uuid NOT NULL REFERENCES client (id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    CHECK ((axis = 'evidence' AND status IN ('unreviewed', 'supported', 'ambiguous', 'rejected'))
        OR (axis = 'identity' AND status IN ('unchecked', 'matched', 'unmatched', 'ambiguous'))),
    CHECK ((basis = 'method') = (method_id IS NOT NULL) AND (method_id IS NULL) = (method_version IS NULL)),
    CHECK ((basis = 'model_judgment') = (decision_id IS NOT NULL)),
    -- A model judgment gives evidence status only. Identity comparison stays mechanical or a review.
    CHECK (basis <> 'model_judgment' OR axis = 'evidence')
);
CREATE INDEX assessment_candidate_idx ON assessment (candidate_id, axis, seq);

ALTER TABLE decision ADD FOREIGN KEY (assessment_id) REFERENCES assessment (id);

CREATE TABLE note (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id uuid REFERENCES campaign (id),
    body        text NOT NULL CHECK (body <> ''),
    created_by  uuid NOT NULL REFERENCES client (id),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX note_campaign_idx ON note (campaign_id, created_at);

-- A unit of agent work. Clients claim tasks with SELECT ... FOR UPDATE SKIP LOCKED and hold a lease, as workers claim jobs.
-- Each claim sets a new lease_id. Only claimed_by with the current lease_id can extend, release, or end the lease.
CREATE TABLE agent_task (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id      uuid NOT NULL REFERENCES campaign (id),
    kind             text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_]{0,62}$'),
    title            text NOT NULL CHECK (title <> ''),
    instructions     text NOT NULL DEFAULT '',
    priority         integer NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
    lease_seconds    integer NOT NULL DEFAULT 900 CHECK (lease_seconds BETWEEN 30 AND 86400),
    state            text NOT NULL DEFAULT 'queued'
                     CHECK (state IN ('queued', 'claimed', 'complete', 'failed', 'cancelled')),
    attempts         integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts     integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
    claimed_by       uuid REFERENCES client (id),
    claimed_at       timestamptz,
    lease_id         uuid,
    lease_expires_at timestamptz,
    summary          text,
    -- Why the task failed or was cancelled.
    reason           text,
    cancelled_by     uuid REFERENCES client (id),
    created_by       uuid NOT NULL REFERENCES client (id),
    created_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    CHECK (state <> 'claimed' OR (claimed_by IS NOT NULL AND claimed_at IS NOT NULL AND lease_id IS NOT NULL
                                  AND lease_expires_at IS NOT NULL)),
    CHECK ((state IN ('complete', 'failed', 'cancelled')) = (finished_at IS NOT NULL)),
    CHECK (state <> 'complete' OR summary <> ''),
    CHECK (state NOT IN ('failed', 'cancelled') OR reason <> ''),
    CHECK ((state = 'cancelled') = (cancelled_by IS NOT NULL))
);
CREATE INDEX agent_task_claim_idx ON agent_task (campaign_id, priority DESC, created_at) WHERE state = 'queued';
CREATE INDEX agent_task_lease_idx ON agent_task (campaign_id, lease_expires_at) WHERE state = 'claimed';

-- A reference from a record to a platform record or an external file.
-- Exactly one source column and exactly one target are set. Foreign keys keep each platform reference valid.
CREATE TABLE record_link (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- evidence: candidate, assessment, and decision evidence. subject: what a note is about.
    -- input and output: what an agent task uses and produces.
    role                 text NOT NULL CHECK (role IN ('evidence', 'subject', 'input', 'output')),
    position             integer NOT NULL CHECK (position >= 0),
    candidate_id         uuid REFERENCES candidate (id),
    assessment_id        uuid REFERENCES assessment (id),
    decision_id          uuid REFERENCES decision (id),
    note_id              uuid REFERENCES note (id),
    agent_task_id        uuid REFERENCES agent_task (id),
    ref_campaign         uuid REFERENCES campaign (id),
    ref_observation      uuid REFERENCES observation (id),
    ref_run              uuid REFERENCES run (id),
    ref_job              uuid REFERENCES job (id),
    ref_measurement      bigint REFERENCES measurement (id),
    ref_artifact         uuid REFERENCES artifact (id),
    ref_candidate        uuid REFERENCES candidate (id),
    ref_assessment       uuid REFERENCES assessment (id),
    ref_decision         uuid REFERENCES decision (id),
    ref_note             uuid REFERENCES note (id),
    ref_agent_task       uuid REFERENCES agent_task (id),
    external_uri         text CHECK (external_uri <> ''),
    external_sha256      text CHECK (external_sha256 ~ '^[0-9a-f]{64}$'),
    external_bytes       bigint CHECK (external_bytes >= 0),
    external_description text CHECK (external_description <> ''),
    CHECK (num_nonnulls(candidate_id, assessment_id, decision_id, note_id, agent_task_id) = 1),
    CHECK (num_nonnulls(ref_campaign, ref_observation, ref_run, ref_job, ref_measurement, ref_artifact, ref_candidate,
                        ref_assessment, ref_decision, ref_note, ref_agent_task, external_uri) = 1),
    CHECK ((external_uri IS NULL) = (external_sha256 IS NULL) AND (external_uri IS NULL) = (external_bytes IS NULL)),
    CHECK (external_description IS NULL OR external_uri IS NOT NULL),
    CHECK ((role = 'evidence') = (num_nonnulls(candidate_id, assessment_id, decision_id) = 1)),
    CHECK ((role = 'subject') = (note_id IS NOT NULL)),
    CHECK ((role IN ('input', 'output')) = (agent_task_id IS NOT NULL))
);
CREATE INDEX record_link_candidate_idx ON record_link (candidate_id) WHERE candidate_id IS NOT NULL;
CREATE INDEX record_link_assessment_idx ON record_link (assessment_id) WHERE assessment_id IS NOT NULL;
CREATE INDEX record_link_decision_idx ON record_link (decision_id) WHERE decision_id IS NOT NULL;
CREATE INDEX record_link_note_idx ON record_link (note_id) WHERE note_id IS NOT NULL;
CREATE INDEX record_link_agent_task_idx ON record_link (agent_task_id) WHERE agent_task_id IS NOT NULL;
CREATE INDEX record_link_ref_candidate_idx ON record_link (ref_candidate) WHERE ref_candidate IS NOT NULL;
CREATE INDEX record_link_ref_decision_idx ON record_link (ref_decision) WHERE ref_decision IS NOT NULL;
CREATE INDEX record_link_ref_assessment_idx ON record_link (ref_assessment) WHERE ref_assessment IS NOT NULL;
CREATE INDEX record_link_ref_note_idx ON record_link (ref_note) WHERE ref_note IS NOT NULL;
CREATE INDEX record_link_ref_agent_task_idx ON record_link (ref_agent_task) WHERE ref_agent_task IS NOT NULL;
CREATE INDEX record_link_ref_observation_idx ON record_link (ref_observation) WHERE ref_observation IS NOT NULL;

-- Rule candidate-status/1: the newest assessment on each axis gives the current status.
-- Without an assessment, the evidence status is unreviewed and the identity status is unchecked.
CREATE VIEW candidate_status AS
SELECT c.id AS candidate_id,
       coalesce(e.status, 'unreviewed') AS evidence_status, e.id AS evidence_assessment_id, e.basis AS evidence_basis,
       coalesce(i.status, 'unchecked') AS identity_status, i.id AS identity_assessment_id, i.basis AS identity_basis
FROM candidate c
LEFT JOIN LATERAL (SELECT id, status, basis FROM assessment
                   WHERE candidate_id = c.id AND axis = 'evidence' ORDER BY seq DESC LIMIT 1) e ON true
LEFT JOIN LATERAL (SELECT id, status, basis FROM assessment
                   WHERE candidate_id = c.id AND axis = 'identity' ORDER BY seq DESC LIMIT 1) i ON true;

-- ---------- Rules ----------

-- Candidates, assessments, decisions, notes, and links do not change.
CREATE FUNCTION record_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% records do not change after the insert. Write a new record.', TG_TABLE_NAME
        USING ERRCODE = 'RC002';
END;
$$;
CREATE TRIGGER candidate_immutable BEFORE UPDATE OR DELETE ON candidate
    FOR EACH ROW EXECUTE FUNCTION record_immutable();
CREATE TRIGGER candidate_observation_immutable BEFORE UPDATE OR DELETE ON candidate_observation
    FOR EACH ROW EXECUTE FUNCTION record_immutable();
CREATE TRIGGER assessment_immutable BEFORE UPDATE OR DELETE ON assessment
    FOR EACH ROW EXECUTE FUNCTION record_immutable();
CREATE TRIGGER decision_immutable BEFORE UPDATE OR DELETE ON decision
    FOR EACH ROW EXECUTE FUNCTION record_immutable();
CREATE TRIGGER note_immutable BEFORE UPDATE OR DELETE ON note
    FOR EACH ROW EXECUTE FUNCTION record_immutable();
CREATE TRIGGER record_link_immutable BEFORE UPDATE OR DELETE ON record_link
    FOR EACH ROW EXECUTE FUNCTION record_immutable();

-- The decision freeze. In a campaign, candidates, evidence assessments, and decisions need an approved campaign
-- whose decisions are not fixed. Identity assessments need a campaign that is not closed: identity comparison
-- follows the fixed decisions. FOR SHARE waits for a concurrent change of the campaign row, so a record
-- and the fixing of decisions cannot both pass.
CREATE FUNCTION research_record_freeze() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    cid uuid;
    c   campaign;
BEGIN
    IF TG_TABLE_NAME = 'candidate' THEN
        cid := NEW.campaign_id;
    ELSE
        SELECT campaign_id INTO cid FROM candidate WHERE id = NEW.candidate_id;
    END IF;
    IF cid IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT * INTO c FROM campaign WHERE id = cid FOR SHARE;
    IF c.closed_at IS NOT NULL THEN
        RAISE EXCEPTION 'campaign % is closed', c.name USING ERRCODE = 'RC001';
    END IF;
    IF TG_TABLE_NAME = 'assessment' THEN
        IF NEW.axis = 'identity' THEN
            RETURN NEW;
        END IF;
    END IF;
    IF c.approved_at IS NULL THEN
        RAISE EXCEPTION 'campaign % is not approved', c.name USING ERRCODE = 'RC001';
    END IF;
    IF c.decisions_fixed_at IS NOT NULL THEN
        RAISE EXCEPTION 'campaign % fixed its decisions. It takes no new candidate, evidence assessment, or decision.',
            c.name USING ERRCODE = 'RC001';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER candidate_freeze BEFORE INSERT ON candidate
    FOR EACH ROW EXECUTE FUNCTION research_record_freeze();
CREATE TRIGGER assessment_freeze BEFORE INSERT ON assessment
    FOR EACH ROW EXECUTE FUNCTION research_record_freeze();
CREATE TRIGGER decision_freeze BEFORE INSERT ON decision
    FOR EACH ROW EXECUTE FUNCTION research_record_freeze();

-- A correction refers to an earlier record of the same candidate. A model-judgment assessment and a decision
-- that uses an assessment refer to the same candidate.
CREATE FUNCTION research_record_consistency() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'assessment' THEN
        IF NEW.supersedes IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM assessment WHERE id = NEW.supersedes AND candidate_id = NEW.candidate_id AND axis = NEW.axis) THEN
            RAISE EXCEPTION 'an assessment supersedes only an assessment of the same candidate and axis' USING ERRCODE = 'RC003';
        END IF;
        IF NEW.decision_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM decision WHERE id = NEW.decision_id AND candidate_id = NEW.candidate_id) THEN
            RAISE EXCEPTION 'the decision of an assessment is about the same candidate' USING ERRCODE = 'RC003';
        END IF;
    ELSE
        IF NEW.supersedes IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM decision WHERE id = NEW.supersedes AND candidate_id = NEW.candidate_id) THEN
            RAISE EXCEPTION 'a decision supersedes only a decision about the same candidate' USING ERRCODE = 'RC003';
        END IF;
        IF NEW.assessment_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM assessment WHERE id = NEW.assessment_id AND candidate_id = NEW.candidate_id) THEN
            RAISE EXCEPTION 'the assessment of a decision is about the same candidate' USING ERRCODE = 'RC003';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER assessment_consistency BEFORE INSERT ON assessment
    FOR EACH ROW EXECUTE FUNCTION research_record_consistency();
CREATE TRIGGER decision_consistency BEFORE INSERT ON decision
    FOR EACH ROW EXECUTE FUNCTION research_record_consistency();

-- The campaign of a record, or null for a record without a campaign.
CREATE FUNCTION record_campaign(kind text, record_id text) RETURNS uuid LANGUAGE sql STABLE AS $$
    SELECT CASE kind
        WHEN 'campaign'    THEN record_id::uuid
        WHEN 'run'         THEN (SELECT campaign_id FROM run WHERE id = record_id::uuid)
        WHEN 'job'         THEN (SELECT r.campaign_id FROM job j JOIN run r ON r.id = j.run_id WHERE j.id = record_id::uuid)
        WHEN 'measurement' THEN (SELECT r.campaign_id FROM measurement m JOIN job j ON j.id = m.job_id
                                 JOIN run r ON r.id = j.run_id WHERE m.id = record_id::bigint)
        WHEN 'artifact'    THEN (SELECT r.campaign_id FROM artifact a JOIN job j ON j.id = a.job_id
                                 JOIN run r ON r.id = j.run_id WHERE a.id = record_id::uuid)
        WHEN 'candidate'   THEN (SELECT campaign_id FROM candidate WHERE id = record_id::uuid)
        WHEN 'assessment'  THEN (SELECT c.campaign_id FROM assessment a JOIN candidate c ON c.id = a.candidate_id
                                 WHERE a.id = record_id::uuid)
        WHEN 'decision'    THEN (SELECT c.campaign_id FROM decision d JOIN candidate c ON c.id = d.candidate_id
                                 WHERE d.id = record_id::uuid)
        WHEN 'note'        THEN (SELECT campaign_id FROM note WHERE id = record_id::uuid)
        WHEN 'agent_task'  THEN (SELECT campaign_id FROM agent_task WHERE id = record_id::uuid)
    END;
$$;

-- A link belongs to its record: it is written in the transaction that writes the record or, for an agent task,
-- the transaction that changes the task. A record of a campaign refers only to records of the same campaign
-- or to records without a campaign.
CREATE FUNCTION record_link_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    source_kind text;
    source_id   uuid;
    target_kind text;
    target_id   text;
    written     boolean;
    source_c    uuid;
    target_c    uuid;
BEGIN
    source_kind := CASE WHEN NEW.candidate_id IS NOT NULL THEN 'candidate' WHEN NEW.assessment_id IS NOT NULL THEN 'assessment'
                        WHEN NEW.decision_id IS NOT NULL THEN 'decision' WHEN NEW.note_id IS NOT NULL THEN 'note'
                        ELSE 'agent_task' END;
    source_id := coalesce(NEW.candidate_id, NEW.assessment_id, NEW.decision_id, NEW.note_id, NEW.agent_task_id);
    EXECUTE format('SELECT xmin = pg_current_xact_id()::xid FROM %I WHERE id = $1', source_kind) INTO written USING source_id;
    IF written IS NOT TRUE THEN
        RAISE EXCEPTION 'a link of % % must be written with the record', source_kind, source_id USING ERRCODE = 'RC003';
    END IF;
    source_c := record_campaign(source_kind, source_id::text);
    IF source_c IS NULL OR NEW.external_uri IS NOT NULL OR NEW.ref_observation IS NOT NULL THEN
        RETURN NEW;
    END IF;
    SELECT k, v INTO target_kind, target_id FROM (VALUES
        ('campaign', NEW.ref_campaign::text), ('run', NEW.ref_run::text), ('job', NEW.ref_job::text),
        ('measurement', NEW.ref_measurement::text), ('artifact', NEW.ref_artifact::text),
        ('candidate', NEW.ref_candidate::text), ('assessment', NEW.ref_assessment::text),
        ('decision', NEW.ref_decision::text), ('note', NEW.ref_note::text), ('agent_task', NEW.ref_agent_task::text)) AS t (k, v)
    WHERE v IS NOT NULL;
    target_c := record_campaign(target_kind, target_id);
    IF target_c IS NOT NULL AND target_c <> source_c THEN
        RAISE EXCEPTION 'a record of campaign % cannot refer to % % of campaign %', source_c, target_kind, target_id, target_c
            USING ERRCODE = 'RC003';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER record_link_check BEFORE INSERT ON record_link
    FOR EACH ROW EXECUTE FUNCTION record_link_check();

-- The observations of a candidate are written with the candidate.
CREATE FUNCTION candidate_observation_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM candidate WHERE id = NEW.candidate_id AND xmin = pg_current_xact_id()::xid) THEN
        RAISE EXCEPTION 'the observations of candidate % must be written with the candidate', NEW.candidate_id
            USING ERRCODE = 'RC003';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER candidate_observation_check BEFORE INSERT ON candidate_observation
    FOR EACH ROW EXECUTE FUNCTION candidate_observation_check();

-- Agent tasks: a closed campaign takes no new task. A finished task does not change.
-- The campaign, writer, and work description of a task do not change.
CREATE FUNCTION agent_task_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'agent tasks are not deleted. Cancel the task.' USING ERRCODE = 'RC002';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF EXISTS (SELECT 1 FROM campaign WHERE id = NEW.campaign_id AND closed_at IS NOT NULL) THEN
            RAISE EXCEPTION 'campaign % is closed', NEW.campaign_id USING ERRCODE = 'RC001';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state IN ('complete', 'failed', 'cancelled') THEN
        RAISE EXCEPTION 'agent task % is %. A finished task does not change.', OLD.id, OLD.state USING ERRCODE = 'RC002';
    END IF;
    IF (NEW.campaign_id, NEW.kind, NEW.title, NEW.instructions, NEW.created_by, NEW.created_at)
            IS DISTINCT FROM (OLD.campaign_id, OLD.kind, OLD.title, OLD.instructions, OLD.created_by, OLD.created_at) THEN
        RAISE EXCEPTION 'the campaign, writer, and work description of agent task % do not change', OLD.id
            USING ERRCODE = 'RC002';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER agent_task_check BEFORE INSERT OR UPDATE OR DELETE ON agent_task
    FOR EACH ROW EXECUTE FUNCTION agent_task_check();

-- The close of a campaign cancels its waiting and claimed agent tasks.
CREATE FUNCTION campaign_close_tasks() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE agent_task SET state = 'cancelled', finished_at = now(), cancelled_by = NEW.closed_by,
                          reason = 'The campaign closed.', lease_expires_at = NULL
    WHERE campaign_id = NEW.id AND state IN ('queued', 'claimed');
    RETURN NEW;
END;
$$;
CREATE TRIGGER campaign_close_tasks AFTER UPDATE OF closed_at ON campaign
    FOR EACH ROW WHEN (OLD.closed_at IS NULL AND NEW.closed_at IS NOT NULL) EXECUTE FUNCTION campaign_close_tasks();
