-- The agents' obligations: what needs judgment, the role that owns it, its
-- deadline, and when the ledger showed it done. `id` is the rule's stable key,
-- so re-deriving an obligation never duplicates it.
CREATE TABLE IF NOT EXISTS agent_tasks (
  id text PRIMARY KEY,
  kind text NOT NULL,
  role text NOT NULL CHECK (role IN ('a', 'b', 'c', 'd')),
  subject text NOT NULL,
  title text NOT NULL,
  context jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
  opened_at timestamptz NOT NULL,
  due_at timestamptz NOT NULL,
  overdue_at timestamptz,
  escalated_at timestamptz,
  done_at timestamptz,
  CHECK ((status = 'done') = (done_at IS NOT NULL)),
  CHECK (due_at >= opened_at)
);
CREATE INDEX IF NOT EXISTS agent_tasks_open_idx ON agent_tasks (role, due_at) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS agent_tasks_escalated_idx ON agent_tasks (escalated_at) WHERE escalated_at IS NOT NULL;
