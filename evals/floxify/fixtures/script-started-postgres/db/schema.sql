-- Schema snapshot. Regenerate with `npm run db:dump` (shells out to pg_dump
-- against the cluster that scripts/start_dev_db.sh starts).
CREATE TABLE IF NOT EXISTS customers (
    id          SERIAL PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
    id          SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers (id),
    total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
    placed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS orders_customer_id_idx ON orders (customer_id);
