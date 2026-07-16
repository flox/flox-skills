# orders-api

A small Express API backed by PostgreSQL.

## Getting started

```bash
npm install
cp .env.example .env
./scripts/start_dev_db.sh   # starts postgres, loads db/schema.sql
npm run dev
```

The app will not start without a database reachable at `DATABASE_URL`.

## Notes on the dev database

`scripts/start_dev_db.sh` runs postgres over a **unix socket inside the repo**
(`target/dev-db-socket`) rather than TCP, so multiple checkouts can run side by
side. The socket path is percent-encoded into `DATABASE_URL` because it contains
`/` characters. The cluster itself lives in `target/dev-db` so that
`npm run clean` wipes it.

Dump the schema after a migration with `npm run db:dump`.
