# Backend image for the API and the workers (#72).
# Build from the repository root: docker compose build, or
#   docker build -f backend/Dockerfile .
FROM python:3.12.15-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/srv/backend \
    PYTHONUNBUFFERED=1

WORKDIR /srv/backend

# Dependencies first, so code changes do not reinstall them.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/app ./app
COPY fixtures /srv/fixtures
COPY contracts/components/c08-allocation/examples /srv/contracts/components/c08-allocation/examples
COPY contracts/components/c05-planning/examples /srv/contracts/components/c05-planning/examples

RUN useradd --create-home --uid 10001 app
USER app

# Each Compose service sets its own command (see docker-compose.yml).
CMD ["python", "-c", "print('Choose a command, e.g. python -m app.components.c07_disruption_detection.worker')"]
