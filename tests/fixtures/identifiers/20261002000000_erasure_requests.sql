-- Platform user-deletion fan-out: one row per Auth `auth.user.deletion_requested`
-- request this service has handled. The request id makes a redelivery a no-op;
-- the evidence is stored so a replay answers with the same rows, and is
-- re-sent until Auth has it. It holds counts and reasons only, never a user id.
CREATE TABLE erasure_requests (
  request_id text PRIMARY KEY,
  evidence jsonb NOT NULL,
  evidence_posted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
