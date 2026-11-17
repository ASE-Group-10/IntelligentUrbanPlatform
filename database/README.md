# Database

PostgreSQL 17 (Supabase in shared environments, a local `postgres:17.7`
container for development). Schema changes are plain SQL migrations applied
with [dbmate](https://github.com/amacneil/dbmate).

## Local commands

Run from the repository root. Requires Docker with Compose.

```sh
cp .env.example .env
docker compose up -d db rabbitmq
docker compose run --rm dbmate up
docker compose run --rm dbmate status
docker compose run --rm dbmate rollback
docker compose down
```

Without Docker, install dbmate (`brew install dbmate`) and run:

```sh
dbmate --migrations-dir database/migrations --no-dump-schema up
```

## Writing a migration

```sh
docker compose run --rm dbmate new <short_description>
```

Each migration must contain `-- migrate:up` and a working `-- migrate:down`
section. Component-owned schemas are created by the component's first
migration. Never edit a migration after it is merged into `develop`; add a new
one instead.

## Shared database

Migrations reach the shared database only from merged `develop`, run by the
database owner. Set `MIGRATION_DATABASE_URL` in `.env`; keep the connection
string out of Git. For Supabase, use the session-pooler connection on port
5432. The direct connection is IPv6-only and the transaction pooler on port
6543 is unsuitable for migrations.
