-- Optional starter schema for Safarz.
-- Use this only when your backend does not already have equivalent tables.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS stops (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  name_ur TEXT,
  aliases TEXT[] NOT NULL DEFAULT '{}',
  latitude NUMERIC(9, 6) NOT NULL,
  longitude NUMERIC(9, 6) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS stops_name_search_idx
  ON stops USING gin (to_tsvector('simple', coalesce(name, '') || ' ' || coalesce(name_ur, '')));

CREATE TABLE IF NOT EXISTS buses (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  route_code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  vehicle_type TEXT NOT NULL CHECK (vehicle_type IN ('ev', 'fuel')),
  has_ac BOOLEAN NOT NULL DEFAULT false,
  has_wheelchair BOOLEAN NOT NULL DEFAULT false,
  active BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS routes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  bus_id UUID NOT NULL REFERENCES buses(id) ON DELETE CASCADE,
  duration_minutes INTEGER NOT NULL,
  frequency_minutes INTEGER,
  fare_label TEXT,
  fare_kind TEXT NOT NULL DEFAULT 'variable' CHECK (fare_kind IN ('fixed', 'variable')),
  geometry JSONB,
  active BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS route_stops (
  route_id UUID NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
  stop_id UUID NOT NULL REFERENCES stops(id) ON DELETE CASCADE,
  stop_order INTEGER NOT NULL,
  PRIMARY KEY (route_id, stop_id),
  UNIQUE (route_id, stop_order)
);

CREATE INDEX IF NOT EXISTS route_stops_lookup_idx
  ON route_stops (stop_id, route_id, stop_order);

-- Example EV fare rows are intentionally stored as a label because the
-- backend can apply distance logic before returning the result:
-- "Rs 80 / Rs 120" for up to 15 km / over 15 km.