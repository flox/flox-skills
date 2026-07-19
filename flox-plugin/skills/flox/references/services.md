# Flox Services Guide

## Running Services in Flox Environments

- Start with `flox activate --start-services` or `flox activate -s`
- Define `is-daemon`, `shutdown.command` for background processes
- Keep services running using `tail -f /dev/null`
- Use `flox services status/logs/restart` to manage (must be in activated env)
- Service commands don't inherit hook activations; explicitly source/activate what you need

## Core Commands

```bash
flox activate -s                # Start services
flox services status            # Check service status
flox services logs <service>    # View service logs
flox services restart <service> # Restart a service
flox services stop <service>    # Stop a service
```

## Network Services Pattern

Always make host/port configurable via vars:

```toml
[services.webapp]
command = '''exec app --host "$APP_HOST" --port "$APP_PORT"'''

[vars]
APP_HOST = "0.0.0.0"  # Network-accessible
APP_PORT = "8080"
```

## Service Logging Pattern

Always pipe to `$FLOX_ENV_CACHE/logs/` for debugging:

```toml
[services.myapp]
command = '''
  mkdir -p "$FLOX_ENV_CACHE/logs"
  exec app 2>&1 | tee -a "$FLOX_ENV_CACHE/logs/app.log"
'''
```

## Python venv Pattern for Services

Services must activate venv independently:

```toml
[services.myapp]
command = '''
  [ -f "$FLOX_ENV_CACHE/venv/bin/activate" ] && \
    source "$FLOX_ENV_CACHE/venv/bin/activate"
  exec python-app "$@"
'''
```

Or use venv Python directly:

```toml
[services.myapp]
command = '''exec "$FLOX_ENV_CACHE/venv/bin/python" app.py'''
```

## Using Packaged Services

Override package's service by redefining with same name.

## Database Service Examples

### Choosing TCP vs. Unix socket

Flox-managed datastores can bind a TCP port, a Unix domain socket, or
both. The right default depends on how the *client* connects, not habit:

- **Unix socket** avoids colliding with a developer's own instance of
  the same datastore already listening on its default port (a second
  Postgres on 5432, a second Redis on 6379). It's transparent for
  env-var-driven clients that already resolve a socket path (libpq's
  `PGHOST`, and anything built on it — `psql`, `psycopg2`, the `pg`
  gem). Point it at a short, project-named path, e.g.
  `/tmp/<project>-postgres` — `$FLOX_ENV_CACHE` is usually too long for
  a Unix socket path (the `AF_UNIX` path limit is 108 bytes on Linux,
  104 on macOS), and a bare `/tmp` default collides with every other
  floxified project on the machine once the socket filename is derived
  from the port.
- **TCP** is required for clients that parse a URL-shaped connection
  string themselves instead of deferring to the driver's socket
  convention (Prisma and similar ORMs reading `DATABASE_URL`/
  `REDIS_URL` directly), and for anything that can't speak Unix sockets
  at all.

Not every datastore below can go socket-only — check each one's note
before dropping TCP.

### PostgreSQL

**Unix socket (default)** — socket-only; `-k` plus
`listen_addresses=''` disables TCP entirely, so this only serves
clients that resolve `PGHOST` to a socket directory (any libpq-based
driver). Pass `-c listen_addresses=''` at both `initdb`/bootstrap time
and in the steady-state `[services.postgres]` command — a fresh
cluster still attempts a TCP bind during its own bootstrap start
otherwise, and that bind fails silently if a host Postgres already
holds 5432.

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

`auth=trust` is dev-only — call it out if this manifest leaves your
machine.

**TCP** — for a client that parses `DATABASE_URL` itself instead of
resolving `PGHOST` (Prisma and similar ORMs), or when nothing else in
the environment needs the socket. If something else still expects the
socket (`psql` invoked without `-h`), keep `-k "$PGHOST"` alongside the
TCP bind instead of dropping it — TCP is additive here, not a
replacement.

```toml
[services.postgres]
command = '''
  mkdir -p "$FLOX_ENV_CACHE/postgres"
  if [ ! -d "$FLOX_ENV_CACHE/postgres/data" ]; then
    initdb -D "$FLOX_ENV_CACHE/postgres/data"
  fi
  exec postgres -D "$FLOX_ENV_CACHE/postgres/data" \
    -h "$POSTGRES_HOST" \
    -p "$POSTGRES_PORT"
'''
is-daemon = true

[vars]
POSTGRES_HOST = "localhost"
POSTGRES_PORT = "5432"
POSTGRES_USER = "myuser"
POSTGRES_DB = "mydb"
```

### Redis

Redis has no equivalent of libpq's `PGHOST` auto-detection — no
standard Redis client reads a socket-path env var, and an app with
`REDIS_URL` unset still falls back to `redis://127.0.0.1:6379`.
**Never run Redis socket-only**; the choice is TCP alone, or TCP with
a socket wired alongside for the app that can use one.

**TCP** — the simple default when nothing on the box needs a socket:

```toml
[services.redis]
command = '''
  mkdir -p "$FLOX_ENV_CACHE/redis"
  exec redis-server \
    --bind "$REDIS_HOST" \
    --port "$REDIS_PORT" \
    --dir "$FLOX_ENV_CACHE/redis"
'''
is-daemon = true

[vars]
REDIS_HOST = "127.0.0.1"
REDIS_PORT = "6379"
```

**TCP + Unix socket** — add `--unixsocket` alongside the TCP bind, not
in place of it, for the client that can use one. Name the socket file
after the project (`myapp-redis.sock`, not a bare `redis.sock`) so it
doesn't collide with another floxified project's socket in `/tmp`.

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

`--save ""` and `--appendonly no` disable persistence — fine for dev,
but data resets on every service stop; drop them if you need
durability across restarts.

### MongoDB

MongoDB doesn't support a socket-only listener the way Postgres
does — `mongod` always binds a TCP port and can *additionally* expose
a Unix socket, the same TCP + socket shape as Redis above.

**TCP** — the simple default:

```toml
[services.mongodb]
command = '''
  mkdir -p "$FLOX_ENV_CACHE/mongodb"
  exec mongod \
    --dbpath "$FLOX_ENV_CACHE/mongodb" \
    --bind_ip "$MONGODB_HOST" \
    --port "$MONGODB_PORT" \
    --nounixsocket
'''
is-daemon = true

[vars]
MONGODB_HOST = "127.0.0.1"
MONGODB_PORT = "27017"
```

**TCP + Unix socket** — `mongod` opens a socket automatically unless
`--nounixsocket` is passed, but its default directory is a bare
`/tmp` — scope it to the project with `--unixSocketPrefix`, the same
way the Postgres and Redis patterns above scope their socket paths:

```toml
[services.mongodb]
command = '''
  mkdir -p "$FLOX_ENV_CACHE/mongodb" /tmp/myapp-mongodb
  exec mongod \
    --dbpath "$FLOX_ENV_CACHE/mongodb" \
    --bind_ip "$MONGODB_HOST" \
    --port "$MONGODB_PORT" \
    --unixSocketPrefix /tmp/myapp-mongodb
'''
is-daemon = true

[vars]
MONGODB_HOST = "127.0.0.1"
MONGODB_PORT = "27017"
```

## Web Server Examples

### Node.js Development Server

```toml
[services.dev-server]
command = '''
  exec npm run dev -- --host "$DEV_HOST" --port "$DEV_PORT"
'''

[vars]
DEV_HOST = "0.0.0.0"
DEV_PORT = "3000"
```

### Python Flask/FastAPI

```toml
[services.api]
command = '''
  source "$FLOX_ENV_CACHE/venv/bin/activate"
  exec python -m uvicorn main:app \
    --host "$API_HOST" \
    --port "$API_PORT" \
    --reload
'''

[vars]
API_HOST = "0.0.0.0"
API_PORT = "8000"
```

### Simple HTTP Server

```toml
[services.web]
command = '''exec python -m http.server "$WEB_PORT"'''

[vars]
WEB_PORT = "8000"
```

## Environment Variable Convention

Use variables like `POSTGRES_HOST`, `POSTGRES_PORT` to define where services run.

These store connection details *separately*:
- `*_HOST` is the hostname or IP address (e.g., `localhost`, `db.example.com`)
- `*_PORT` is the network port number (e.g., `5432`, `6379`)

Postgres's socket form (see PostgreSQL above) is the one exception:
`PGHOST` holds a socket **directory**, not a hostname — that's standard
libpq behavior, and any driver built on it resolves it automatically.

This pattern ensures users can override them at runtime:
```bash
POSTGRES_HOST=db.internal POSTGRES_PORT=6543 flox activate -s
```

Use consistent naming across services so the meaning is clear to any system or person reading the variables.

## Service with Shutdown Command

```toml
[services.myapp]
command = '''exec myapp start'''
is-daemon = true

[services.myapp.shutdown]
command = '''myapp stop'''
```

## Dependent Services

Services can wait for other services to be ready. This example uses
the Unix-socket Postgres form above (`$PGHOST` is a directory); swap in
`$POSTGRES_HOST`/`$POSTGRES_PORT` here if this environment uses the TCP
form instead:

```toml
[services.db]
command = '''
  exec postgres -D "$FLOX_ENV_CACHE/postgres" \
    -k "$PGHOST" \
    -c listen_addresses=""
'''
is-daemon = true

[services.api]
command = '''
  # Wait for database
  until pg_isready -h "$PGHOST" -p "$PGPORT"; do
    sleep 1
  done
  exec python -m uvicorn main:app
'''

[vars]
PGHOST = "/tmp/myapp-postgres"
PGPORT = "5432"
```

## Service Health Checks

```toml
[services.api]
command = '''
  # Health check function
  health_check() {
    curl -sf http://localhost:8000/health > /dev/null
  }

  exec python -m uvicorn main:app --host 0.0.0.0 --port 8000
'''
```

## Best Practices

- Log service output to `$FLOX_ENV_CACHE/logs/`
- Test activation with `flox activate -- <command>` before adding to services
- When debugging services, run the exact command from manifest manually first
- Always make host/port configurable via vars for network services
- Use `exec` to replace the shell process with the service command
- Services must activate venv inside service command, not rely on hook activation
- Use `is-daemon = true` for background processes that should detach

## Debugging Service Issues

### Check Service Status
```bash
flox services status
```

### View Service Logs
```bash
flox services logs myservice
```

### Run Service Command Manually
```bash
flox activate
# Copy the exact command from manifest and run it
```

### Check if Service is Listening
```bash
# Check if port is open
lsof -i :8000
netstat -an | grep 8000

# Test connection
curl http://localhost:8000
nc -zv localhost 8000
```

## Common Pitfalls

### Services Don't Preserve State
Services see fresh environment (no preserved state between restarts). Store persistent data in `$FLOX_ENV_CACHE`.

### Service Commands Don't Inherit Hook Activations
Explicitly source/activate what you need inside the service command.

### Forgetting to Create Directories
Always `mkdir -p` for data directories in service commands.

### Port Conflicts
Use configurable ports via variables to avoid conflicts with other
services. For datastores specifically, prefer a Unix socket (see
Database Service Examples) — it sidesteps the conflict entirely instead
of just making it configurable.

