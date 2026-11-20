# Database

PostgreSQL 17 (Supabase in shared environments, a local `postgres:17.7` container for
development). Schema changes are plain SQL migrations applied with
[dbmate](https://github.com/amacneil/dbmate).

## Local commands

Run from the repository root. Requires Docker with Compose.

```sh
cp .env.example .env                  # once
docker compose up -d db rabbitmq      # start Postgres and RabbitMQ
docker compose run --rm dbmate up     # apply pending migrations
docker compose run --rm dbmate status # list applied/pending migrations
docker compose run --rm dbmate rollback  # undo the latest migration
docker compose down                   # stop (add -v to delete local data)
```

Without Docker, install dbmate (`brew install dbmate`) and run, for example,
`dbmate --migrations-dir database/migrations --no-dump-schema up` with
`DATABASE_URL` set.

## Writing a migration

```sh
docker compose run --rm dbmate new <short_description>
```

This creates `database/migrations/<UTC timestamp>_<short_description>.sql`.

- Each file has a `-- migrate:up` and a working `-- migrate:down` section.
- Each component owns one schema named after it, e.g. `c06_city_monitoring`,
  created in that component's first migration. Only change your own schema;
  read other components' data through their interfaces.
- Never edit a migration once it is merged into `develop`; add a new one.
- Coordinate migrations that touch shared objects before starting.

## Shared (Supabase) database

Migrations reach the shared database only from merged `develop`, run by the
database owner. Set `MIGRATION_DATABASE_URL` in `.env` (see `.env.example`)
and run `docker compose run --rm dbmate up`. The connection string is a
secret: keep it in `.env` or the team secret store, never in Git.

## Local RabbitMQ

The `rabbitmq` service provides an isolated broker for local development and
tests. It exposes AMQP on port `5672` and the management UI on
`http://localhost:15672` using the credentials in `.env.example`.

Applications running on the host connect with `RABBITMQ_URL`. Applications
running in Compose should use `amqp://ulop:ulop@rabbitmq:5672/%2F` instead.
CloudAMQP can be used for shared environments by replacing `RABBITMQ_URL` in
the environment; never commit that URL or its credentials.
