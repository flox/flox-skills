#!/usr/bin/env bash
# Starts the local development database.
#
# Run this before `npm run dev`:
#     ./scripts/start_dev_db.sh
#
# The cluster lives under ./target/dev-db so it is wiped by `npm run clean`.
# We run postgres over a unix socket in the repo tree (not TCP) so several
# checkouts can run side by side without fighting over port 5432.
set -euo pipefail

PGDATA="$PWD/target/dev-db"
SOCKET_DIR="$PWD/target/dev-db-socket"
DB_NAME=orders_development
DB_USER=orders

mkdir -p "$SOCKET_DIR"

if [ ! -d "$PGDATA" ]; then
  initdb -D "$PGDATA" --username="$DB_USER" --auth=trust --no-locale --encoding=UTF8
  # Keep the cluster socket-only; the repo's tooling always connects via the
  # socket path below.
  echo "listen_addresses = ''" >> "$PGDATA/postgresql.conf"
  echo "unix_socket_directories = '$SOCKET_DIR'" >> "$PGDATA/postgresql.conf"
fi

pg_ctl -D "$PGDATA" -l "$PGDATA/postgres.log" start
until pg_isready -h "$SOCKET_DIR" -q; do sleep 0.2; done

createdb -h "$SOCKET_DIR" -U "$DB_USER" "$DB_NAME" 2>/dev/null || true

# Load the checked-in schema snapshot (kept in sync via `npm run db:dump`,
# which shells out to pg_dump against this same cluster).
if [ -f db/schema.sql ]; then
  psql -h "$SOCKET_DIR" -U "$DB_USER" -d "$DB_NAME" -q -f db/schema.sql
fi

# The socket dir contains '/' characters, so it must be percent-encoded before
# it can be embedded in a libpq connection URI as the host component.
ENCODED_SOCKET="$(printf '%s' "$SOCKET_DIR" | jq -sRr @uri)"
echo "DATABASE_URL=postgres://${DB_USER}@${ENCODED_SOCKET}/${DB_NAME}"
echo "Database ready (socket: $SOCKET_DIR)"
