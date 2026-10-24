-- Archive transfers share the worker process and durable database queue.
CREATE TABLE acquisition (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 campaign_id uuid REFERENCES investigation(campaign_id),
 request jsonb NOT NULL CHECK(request->>'contract'='acquisition-request/1'),
 state text NOT NULL DEFAULT 'querying' CHECK(state IN ('querying','preview','queued','downloading','complete','failed','cancelled')),
 manifest jsonb CHECK(manifest->>'contract'='acquisition-manifest/1'),
 manifest_sha256 text CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'),
 approved_by uuid REFERENCES client(id),
 approved_at timestamptz,
 created_by uuid NOT NULL REFERENCES client(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 result jsonb CHECK(result->>'contract'='acquisition-result/1'),
 error text,
 CHECK((manifest IS NULL) = (manifest_sha256 IS NULL)),
 CHECK(state NOT IN ('queued','downloading','complete') OR (approved_by IS NOT NULL AND approved_at IS NOT NULL))
);
CREATE FUNCTION acquisition_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.request IS DISTINCT FROM OLD.request OR NEW.manifest IS DISTINCT FROM OLD.manifest AND OLD.manifest IS NOT NULL THEN
  RAISE EXCEPTION 'An acquisition keeps its request and preview manifest.' USING ERRCODE='RC003';
 END IF;
 IF NEW.manifest_sha256 IS DISTINCT FROM OLD.manifest_sha256 AND OLD.manifest_sha256 IS NOT NULL THEN
  RAISE EXCEPTION 'An acquisition keeps its manifest checksum.' USING ERRCODE='RC003';
 END IF;
 IF NEW.approved_by IS NOT NULL AND NOT EXISTS(SELECT 1 FROM client WHERE id=NEW.approved_by AND kind='person') THEN
  RAISE EXCEPTION 'Only a person can approve an acquisition.' USING ERRCODE='RC001';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER acquisition_guard BEFORE UPDATE ON acquisition FOR EACH ROW EXECUTE FUNCTION acquisition_guard();
CREATE INDEX acquisition_queue ON acquisition(created_at) WHERE state IN ('querying','queued','downloading');
