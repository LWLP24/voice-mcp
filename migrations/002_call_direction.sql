ALTER TABLE calls
  ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'outbound';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'calls_direction_check'
  ) THEN
    ALTER TABLE calls
      ADD CONSTRAINT calls_direction_check
      CHECK (direction IN ('outbound', 'inbound'));
  END IF;
END
$$;
