-- Client identity and permissions (ROADMAP R8, R4 access control).
-- Each API request carries a bearer token. The API stores only the SHA-256 of a token.
-- Each write records the client that made it.

CREATE TABLE client (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name         text NOT NULL UNIQUE CHECK (name ~ '^[a-z][a-z0-9-]{1,62}$'),
    kind         text NOT NULL CHECK (kind IN ('person', 'agent', 'service')),
    permissions  text[] NOT NULL DEFAULT '{}'
                 CHECK (permissions <@ ARRAY['read', 'run.submit', 'record.write', 'acquire',
                                             'identity.reveal', 'admin']::text[]),
    token_sha256 text NOT NULL UNIQUE CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    note         text,
    created_by   text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    disabled_at  timestamptz,
    -- Richard's decision: an agent never holds acquisition, identity reveal, or administration.
    CHECK (kind <> 'agent' OR NOT (permissions && ARRAY['acquire', 'identity.reveal', 'admin']::text[]))
);

-- The client that created a run. Runs from before this migration and runs that a worker creates have none.
ALTER TABLE run ADD COLUMN client_id uuid REFERENCES client (id);
CREATE INDEX run_client_idx ON run (client_id);

-- The client that cancelled a job.
ALTER TABLE job ADD COLUMN cancelled_by uuid REFERENCES client (id);
