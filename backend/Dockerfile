FROM python:3.12.15-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/srv/backend \
    PYTHONUNBUFFERED=1

WORKDIR /srv/backend

COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/app ./app
COPY fixtures /srv/fixtures

RUN useradd --create-home --uid 10001 app
USER app

CMD ["python", "-c", "print('Choose a service command in Compose')"]
