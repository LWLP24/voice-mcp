CREATE TABLE IF NOT EXISTS call_transcript_turns (
  id BIGSERIAL PRIMARY KEY,
  call_id TEXT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
  sequence BIGINT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  text TEXT NOT NULL,
  interrupted BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (call_id, sequence)
);

CREATE INDEX IF NOT EXISTS call_transcript_turns_call_created_at_idx
  ON call_transcript_turns(call_id, created_at, sequence);

CREATE INDEX IF NOT EXISTS calls_principal_started_at_idx
  ON calls(principal_id, (COALESCE(connected_at, created_at)) DESC, id DESC);

WITH historical_turns AS (
  SELECT
    call_id,
    ROW_NUMBER() OVER (PARTITION BY call_id ORDER BY sequence)::BIGINT AS transcript_sequence,
    CASE
      WHEN type = 'call.assistant_transcript_final' THEN 'assistant'
      ELSE 'user'
    END AS role,
    payload->>'transcript' AS text,
    COALESCE((payload->>'interrupted')::BOOLEAN, FALSE) AS interrupted,
    created_at
  FROM call_events
  WHERE type IN ('call.user_transcript_final', 'call.assistant_transcript_final')
    AND NULLIF(payload->>'transcript', '') IS NOT NULL
)
INSERT INTO call_transcript_turns (
  call_id,
  sequence,
  role,
  text,
  interrupted,
  created_at
)
SELECT
  call_id,
  transcript_sequence,
  role,
  text,
  interrupted,
  created_at
FROM historical_turns
ON CONFLICT (call_id, sequence) DO NOTHING;
