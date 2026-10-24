ALTER TABLE agent_session DROP CONSTRAINT agent_session_document_check;
ALTER TABLE agent_session ADD CONSTRAINT agent_session_document_check
 CHECK (document->>'contract' IN ('agent-session/1', 'agent-session/2'));
