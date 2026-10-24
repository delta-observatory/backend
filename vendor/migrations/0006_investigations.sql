-- Product control uses the existing campaign identity and scientific records.
-- Plan revisions, approvals, execution links, and events retain their original contents.
CREATE TABLE investigation (
    campaign_id uuid PRIMARY KEY REFERENCES campaign (id),
    state text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'ready', 'paused', 'stopped')),
    head_revision_id uuid,
    approved_revision_id uuid,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE investigation_revision (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id uuid NOT NULL REFERENCES investigation (campaign_id),
    number integer NOT NULL CHECK (number > 0),
    parent_id uuid,
    document jsonb NOT NULL CHECK (document ->> 'contract' = 'investigation-plan/1'),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    created_by uuid NOT NULL REFERENCES client (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, number),
    UNIQUE (campaign_id, id),
    FOREIGN KEY (campaign_id, parent_id) REFERENCES investigation_revision (campaign_id, id)
);
ALTER TABLE investigation ADD CONSTRAINT investigation_head
    FOREIGN KEY (campaign_id, head_revision_id) REFERENCES investigation_revision (campaign_id, id);
ALTER TABLE investigation ADD CONSTRAINT investigation_approved
    FOREIGN KEY (campaign_id, approved_revision_id) REFERENCES investigation_revision (campaign_id, id);

CREATE TABLE investigation_approval (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES investigation (campaign_id),
    revision_id uuid NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    approved_by uuid NOT NULL REFERENCES client (id),
    approved_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (campaign_id, revision_id) REFERENCES investigation_revision (campaign_id, id)
);

CREATE TABLE investigation_execution (
    campaign_id uuid NOT NULL REFERENCES investigation (campaign_id),
    request_key text NOT NULL CHECK (length(request_key) BETWEEN 1 AND 120),
    revision_id uuid NOT NULL,
    run_id uuid NOT NULL UNIQUE REFERENCES run (id),
    created_by uuid NOT NULL REFERENCES client (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, request_key),
    FOREIGN KEY (campaign_id, revision_id) REFERENCES investigation_revision (campaign_id, id)
);

CREATE TABLE investigation_event (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES investigation (campaign_id),
    document jsonb NOT NULL CHECK (document ->> 'contract' = 'investigation-event/1'),
    created_by uuid NOT NULL REFERENCES client (id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX investigation_event_campaign ON investigation_event (campaign_id, id);

CREATE TRIGGER investigation_revision_immutable BEFORE UPDATE OR DELETE ON investigation_revision
    FOR EACH ROW EXECUTE FUNCTION record_immutable();
CREATE TRIGGER investigation_approval_immutable BEFORE UPDATE OR DELETE ON investigation_approval
    FOR EACH ROW EXECUTE FUNCTION record_immutable();
CREATE TRIGGER investigation_execution_immutable BEFORE UPDATE OR DELETE ON investigation_execution
    FOR EACH ROW EXECUTE FUNCTION record_immutable();
CREATE TRIGGER investigation_event_immutable BEFORE UPDATE OR DELETE ON investigation_event
    FOR EACH ROW EXECUTE FUNCTION record_immutable();

CREATE FUNCTION investigation_approval_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM client WHERE id = NEW.approved_by AND kind = 'person') THEN
        RAISE EXCEPTION 'Only a person can approve an investigation plan.' USING ERRCODE = 'RC001';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM investigation_revision WHERE id = NEW.revision_id
                   AND campaign_id = NEW.campaign_id AND sha256 = NEW.sha256) THEN
        RAISE EXCEPTION 'The approval checksum must match the plan revision.' USING ERRCODE = 'RC003';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER investigation_approval_check BEFORE INSERT ON investigation_approval
    FOR EACH ROW EXECUTE FUNCTION investigation_approval_check();
