-- Older deployments may have created call_events.call_id as UUID. CallTool IDs
-- are opaque strings (for example call_01K...), so all call foreign keys use TEXT.
DO $$
DECLARE
  call_id_type TEXT;
BEGIN
  SELECT data_type
    INTO call_id_type
    FROM information_schema.columns
   WHERE table_schema = current_schema()
     AND table_name = 'call_events'
     AND column_name = 'call_id';

  IF call_id_type = 'uuid' THEN
    ALTER TABLE call_events
      ALTER COLUMN call_id TYPE TEXT USING call_id::text;
  END IF;
END
$$;
