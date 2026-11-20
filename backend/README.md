# Backend

Python/FastAPI core for components C02–C12. Requires Python 3.12+ and
[uv](https://docs.astral.sh/uv/).

```sh
cd backend
uv sync            # install pinned dependencies from uv.lock
uv run pytest      # tests (contract examples are read from ../contracts/components/*/examples)
uv run ruff check . && uv run ruff format --check .
```

## Postgres-backed tests

Stores with a Postgres implementation (C06 so far) run the same tests against
the in-memory test double and against a real database. Those tests are skipped
unless `TEST_DATABASE_URL` points at a **dedicated test database**: its schemas
are rebuilt from `database/migrations/` and its tables are emptied before each
test. The test setup refuses any database whose name does not end in `_test`.

Locally, with the Docker Compose database running (`docker compose up -d db`):

```sh
docker compose exec db createdb -U ulop ulop_test   # once
cd backend
TEST_DATABASE_URL=postgres://ulop:ulop@localhost:5432/ulop_test uv run pytest
```

CI starts its own Postgres service and sets `TEST_DATABASE_URL` (see
`.github/workflows/backend.yml`).

## RabbitMQ tests

Publisher tests that need a real broker are skipped unless `TEST_RABBITMQ_URL`
is set. They use a throwaway exchange, never the shared `ulop.events`:

```sh
docker compose up -d rabbitmq
TEST_RABBITMQ_URL=amqp://ulop:ulop@localhost:5672/%2F uv run pytest
```

## C06 outbox publisher

Sends queued `CityStateUpdated` events from the C06 outbox to the `ulop.events`
topic exchange (routing key = event type), at least once. Run it as a worker
next to the database and broker:

```sh
DATABASE_URL=postgres://ulop:ulop@localhost:5432/ulop \
RABBITMQ_URL=amqp://ulop:ulop@localhost:5672/%2F \
uv run python -m app.components.c06_city_monitoring.publisher
```

Stop it with Ctrl+C. Events stay in the outbox while the broker is down, or
while no consumer queue is bound for their event type yet (for example C06
started before C07 on a fresh broker), and go out once that changes.

## C07 worker

Consumes `CityStateUpdated` from `ulop.events` (queue `c07.city-state-updated`)
and runs `evaluateChange`. Until C05 exists it uses the labelled stub in
`planning_stub.py`, which knows one demo plan on `seg:4471023`.

```sh
RABBITMQ_URL=amqp://ulop:ulop@localhost:5672/%2F \
uv run python -m app.components.c07_disruption_detection.worker
```

Workers log one JSON object per line with `component` and, for events,
`correlationId`, `eventId`, `eventType`, `scope` and `runId`. Following one
`correlationId` across the C06 publisher and the C07 worker shows a closure
going through the whole chain.

Other components can consume events the same way with
`app.shared.events.EventConsumer` (see its docstring). Add your queue and its
event types to `app/shared/topology.py` too: publishers declare those queues at
startup, so your queue keeps events published before your worker starts.

## Demo: road closure through C06 and C07

The thin-slice event chain (#72 hops 7–8), using the shared scenario in
`fixtures/dublin-scenario.json`. With `docker compose up -d db rabbitmq` running
and the migrations applied, start the two workers in separate terminals (see
above), then inject the closure:

```sh
DATABASE_URL=postgres://ulop:ulop@localhost:5432/ulop uv run python -m app.demo.feed
```

It prints the run, the `correlationId`, the new City State version and the
queued event ID. The C06 publisher and C07 worker log the same `correlationId`
as the event goes through. Running it again changes nothing; add `--fresh` to
inject a new closure with new IDs at the current time.
