# Datastore service patterns

**Pattern: PostgreSQL as a Flox service**

Use ONLY when docker-compose does NOT already manage postgres.

**Default to a unix socket, not TCP.** A TCP bind on `127.0.0.1:5432`
collides with a developer's own host Postgres, which defaults to the exact
same port — floxify exists to remove that kind of manual conflict, not
recreate it. `-k` plus `listen_addresses=""` disables TCP entirely and
scopes the connection to a socket file instead. Name the socket directory
after the project (`/tmp/myapp-postgres`, not bare `/tmp`) — Postgres's
socket filename is derived from the port (`.s.PGSQL.5432`), so two
floxified projects both defaulting to port 5432 would still collide on a
bare `/tmp` even with TCP disabled.

```toml
[install]
postgresql_16.pkg-path = "postgresql_16"

[vars]
PGHOST = "/tmp/myapp-postgres"
PGPORT = "5432"
PGUSER = "postgres"
PGDATABASE = "myapp_dev"

[hook]
on-activate = '''
  mkdir -p "$PGHOST"
  if [ ! -d "$FLOX_ENV_CACHE/postgres" ]; then
    initdb -D "$FLOX_ENV_CACHE/postgres" \
      --username="$PGUSER" \
      --auth=trust \
      --no-locale \
      --encoding=UTF8 >&2
    pg_ctl -D "$FLOX_ENV_CACHE/postgres" \
      -o "-p $PGPORT -k $PGHOST -c listen_addresses=''" \
      -l "$FLOX_ENV_CACHE/postgres/init.log" start
    until pg_isready -h "$PGHOST" -p "$PGPORT" -q; do sleep 0.2; done
    createdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$PGDATABASE"
    pg_ctl -D "$FLOX_ENV_CACHE/postgres" stop -m fast
  fi
'''

[services.postgres]
command = '''
  exec postgres \
    -D "$FLOX_ENV_CACHE/postgres" \
    -p "$PGPORT" \
    -k "$PGHOST" \
    -c listen_addresses=""
'''
```

The one-time init inside `on-activate` needs `-c listen_addresses=''` too
(not just the steady-state `[services.postgres]` block) — without it, the
FRESH cluster still tries to bind TCP 5432 during `initdb`'s bootstrap
start, and in the exact scenario this pattern targets (a host Postgres
already holding 5432) that bind fails silently and the following `until
pg_isready` loop hangs first activation forever. (Live-verified: `pg_ctl
-o "... -c listen_addresses=''"` starts socket-only, confirmed against a
real `flox activate`-managed postgresql_16 on 2026-07-18.)

`PGHOST` set to a directory (not a hostname) is standard libpq — any driver
built on it (`psql`, the `pg` gem, `psycopg2`) resolves it to the socket
automatically, no app-side change needed. This is transparent for
libpq-style consumers only: an app with an explicit
`DATABASE_URL=postgres://user:pass@localhost:5432/db` that it parses
itself (Prisma and similar ORMs) ignores `PGHOST` entirely and tries the
now-disabled TCP port — check how the app actually connects before
assuming the socket alone is enough; see the TCP-exception case below for
what to do when it doesn't. `auth=trust` is dev-only — always note in the
report: `(dev-only auth — not safe for shared machines)`

**TCP is a deliberate, recorded exception — add it alongside the socket,
never in place of it.** Some apps need a `host:port` connection string —
a driver that can't parse a socket-directory DSN, or a codebase that must
keep a `DATABASE_URL` byte-identical to a documented default — even though
the socket stays useful for everything else. When that applies, keep `-k
"$PGHOST"` and add a literal TCP bind next to it (`-h 127.0.0.1 -p
"$PGPORT"`, or `-c listen_addresses="127.0.0.1"`) — don't repurpose
`PGHOST` into a hostname, which drops the socket the rest of the pattern
depends on. Record the reason in a comment next to `[services.postgres]`,
the same recorded-reason discipline as an exact version pin (see
"Version-pinning discipline" above). Lemmy is the worked example
(`evals/floxify/expected/lemmy.toml:84`): `exec postgres -D "$PGDATA"
-k "$PGSOCKET" -h 127.0.0.1 -p 5432` serves the socket AND loopback TCP
together, because `LEMMY_DATABASE_URL` must stay byte-identical to the
repo's own `postgres://lemmy:password@localhost:5432/lemmy` default —
avoiding fragile socket-path URL encoding across lemmy's two DB drivers,
not because either driver is hard-incapable of a socket. (Live-verified:
`postgres -k <dir> -h 127.0.0.1 -p <port>` accepts connections on both
transports simultaneously, confirmed 2026-07-18.)

Redis service (add alongside postgres when both are needed):

```toml
[install]
redis.pkg-path = "redis"

[vars]
REDIS_URL = "redis://localhost:6379"

[hook]
on-activate = '''
  mkdir -p "$FLOX_ENV_CACHE/redis"
'''

[services.redis]
command = '''
  exec redis-server \
    --port 6379 \
    --unixsocket /tmp/myapp-redis.sock \
    --dir "$FLOX_ENV_CACHE/redis" \
    --save "" \
    --appendonly no
'''
```

**Default to TCP on 6379 with a unix socket wired alongside it — not
socket-only.** Unlike libpq, no standard Redis client reads a socket-path
env var the way `psql` reads `PGHOST`; an app with `REDIS_URL` unset still
falls back to `redis://127.0.0.1:6379`, so disabling TCP breaks the common
case instead of fixing a collision. Every Redis golden confirms this:
mastodon, posthog, and sentry all keep `--port 6379` and add `--unixsocket`
beside it (`evals/floxify/expected/mastodon.toml`, `posthog.toml`,
`sentry.toml`); firefly-iii keeps TCP with no socket at all
(`evals/floxify/expected/firefly-iii.toml`) — zero goldens run
socket-only. `--unixsocket` is there for the app that CAN use one — name
the socket file after the project (`myapp-redis.sock`, not a generic
`redis.sock`) so it doesn't collide with another floxified project's
socket in `/tmp`. `--save ""` and `--appendonly no` disable persistence
(dev-appropriate). Always note in the report: `(no persistence — data
resets on service stop; edit manifest if you need durability)`

> Background on TCP-vs-socket tradeoffs across datastores (incl. MongoDB):
> flox skill `references/services.md` § Database Service Examples.
