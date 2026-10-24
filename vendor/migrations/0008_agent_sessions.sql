CREATE TABLE agent_session (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 campaign_id uuid NOT NULL REFERENCES investigation(campaign_id),
 client_id uuid NOT NULL UNIQUE REFERENCES client(id),
 document jsonb NOT NULL CHECK(document->>'contract'='agent-session/1'),
 created_by uuid NOT NULL REFERENCES client(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX agent_session_campaign ON agent_session(campaign_id,created_at);
