-- Campaigns, campaign budgets, blinding, and access events (ROADMAP R8).
-- An agent client belongs to one campaign. The campaign sets its blinding level and its budgets.

CREATE TABLE campaign (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name                 text NOT NULL UNIQUE CHECK (name ~ '^[a-z][a-z0-9-]{1,62}$'),
    title                text NOT NULL CHECK (title <> ''),
    question             text NOT NULL CHECK (question <> ''),
    -- open: nothing withheld. labels: known-object labels withheld.
    -- labels_identity: labels and catalog identity withheld. Richard's decision makes this the default.
    blinding             text NOT NULL DEFAULT 'labels_identity'
                         CHECK (blinding IN ('open', 'labels', 'labels_identity')),
    -- Limits that the API enforces. Each key is optional. A missing key means no limit.
    budgets              jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(budgets) = 'object'),
    campaign_file_sha256 text CHECK (campaign_file_sha256 ~ '^[0-9a-f]{64}$'),
    created_by           uuid NOT NULL REFERENCES client (id),
    created_at           timestamptz NOT NULL DEFAULT now(),
    approved_by          uuid REFERENCES client (id),
    approved_at          timestamptz,
    decisions_fixed_by   uuid REFERENCES client (id),
    decisions_fixed_at   timestamptz,
    revealed_by          uuid REFERENCES client (id),
    revealed_at          timestamptz,
    reveal_reason        text,
    closed_by            uuid REFERENCES client (id),
    closed_at            timestamptz,
    CHECK ((approved_by IS NULL) = (approved_at IS NULL)),
    CHECK ((decisions_fixed_by IS NULL) = (decisions_fixed_at IS NULL)),
    CHECK ((revealed_by IS NULL) = (revealed_at IS NULL)),
    CHECK ((closed_by IS NULL) = (closed_at IS NULL)),
    -- The order of events: approval, decisions fixed, reveal.
    CHECK (decisions_fixed_at IS NULL OR approved_at IS NOT NULL),
    CHECK (revealed_at IS NULL OR (decisions_fixed_at IS NOT NULL AND reveal_reason <> '')),
    CHECK (approved_at IS NULL OR campaign_file_sha256 IS NOT NULL)
);

ALTER TABLE client ADD COLUMN campaign_id uuid REFERENCES campaign (id);
-- Every agent belongs to a campaign.
ALTER TABLE client ADD CONSTRAINT client_agent_campaign CHECK (kind <> 'agent' OR campaign_id IS NOT NULL);

ALTER TABLE run ADD COLUMN campaign_id uuid REFERENCES campaign (id);
CREATE INDEX run_campaign_idx ON run (campaign_id);

-- The distinct observations whose data a campaign read. The observation budget counts these rows.
CREATE TABLE campaign_read (
    campaign_id    uuid NOT NULL REFERENCES campaign (id),
    observation_id uuid NOT NULL REFERENCES observation (id),
    client_id      uuid NOT NULL REFERENCES client (id),
    first_read_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, observation_id)
);

-- A read, or a refused read, of an observation that a cohort reservation protects.
-- reservation_id names the entry in experiments/cohorts.json.
CREATE TABLE access_event (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    utc            timestamptz NOT NULL DEFAULT now(),
    client_id      uuid NOT NULL REFERENCES client (id),
    campaign_id    uuid REFERENCES campaign (id),
    observation_id uuid NOT NULL REFERENCES observation (id),
    reservation_id text NOT NULL,
    outcome        text NOT NULL CHECK (outcome IN ('allowed', 'refused')),
    operation      text NOT NULL
);
CREATE INDEX access_event_observation_idx ON access_event (observation_id, utc);
