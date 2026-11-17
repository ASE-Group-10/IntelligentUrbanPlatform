# Backend

Python/FastAPI core for components C02–C12. Requires Python 3.12+ and
[uv](https://docs.astral.sh/uv/).

```sh
cd backend
uv sync            # install pinned dependencies from uv.lock
uv run pytest      # tests (contract examples are read from ../contracts/examples)
uv run ruff check . && uv run ruff format --check .
```
