CREATE TABLE IF NOT EXISTS calls (
  id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  client_request_id TEXT,
  status TEXT NOT NULL,
  phase TEXT,
  target_number TEXT NOT NULL,
  request JSONB NOT NULL,
  state JSONB NOT NULL,
  outcome JSONB,
  error JSONB,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  connected_at TIMESTAMPTZ,
  ended_at TIMESTAMPTZ,
  UNIQUE (principal_id, client_request_id)
);

CREATE TABLE IF NOT EXISTS call_events (
  id BIGSERIAL PRIMARY KEY,
  call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
  sequence BIGINT NOT NULL,
  type TEXT NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (call_id, sequence)
);

CREATE INDEX IF NOT EXISTS call_events_call_id_created_at_idx
  ON call_events(call_id, created_at);

CREATE TABLE IF NOT EXISTS input_requests (
  id TEXT PRIMARY KEY,
  call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  request JSONB NOT NULL,
  response JSONB,
  created_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ,
  resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS input_requests_call_id_status_idx
  ON input_requests(call_id, status);

CREATE TABLE IF NOT EXISTS commitments (
  id TEXT PRIMARY KEY,
  call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
  action TEXT NOT NULL,
  payload JSONB NOT NULL,
  allowed BOOLEAN NOT NULL,
  reason TEXT,
  created_at TIMESTAMPTZ NOT NULL
);
