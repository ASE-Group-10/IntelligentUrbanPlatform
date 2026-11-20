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
