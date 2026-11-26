#!/usr/bin/env bash
# Deploy a verified GHCR backend image to the Azure development VM (#75).
#
# Intended to run either via Azure VM Run Command (GitHub Actions OIDC) or
# manually with sudo on the VM. Keeps runtime secrets in $DEPLOY_DIR/.env and
# never prints them.
set -euo pipefail

# Accept positional args (from Azure VM Run Command --parameters), KEY=VALUE args, or env vars.
positional=()
for arg in "$@"; do
  case "${arg}" in
    IMAGE_TAG=*) IMAGE_TAG="${arg#IMAGE_TAG=}" ;;
    EXPECTED_MIGRATIONS=*) EXPECTED_MIGRATIONS="${arg#EXPECTED_MIGRATIONS=}" ;;
    DEPLOY_DIR=*) DEPLOY_DIR="${arg#DEPLOY_DIR=}" ;;
    IMAGE_REPO=*) IMAGE_REPO="${arg#IMAGE_REPO=}" ;;
    HEALTH_URL=*) HEALTH_URL="${arg#HEALTH_URL=}" ;;
    *) positional+=("${arg}") ;;
  esac
done

if [[ "${#positional[@]}" -ge 1 && -z "${IMAGE_TAG:-}" ]]; then
  IMAGE_TAG="${positional[0]}"
fi
if [[ "${#positional[@]}" -ge 2 && -z "${EXPECTED_MIGRATIONS:-}" ]]; then
  EXPECTED_MIGRATIONS="${positional[1]}"
fi
if [[ "${#positional[@]}" -ge 3 && -z "${DEPLOY_DIR:-}" ]]; then
  DEPLOY_DIR="${positional[2]}"
fi

DEPLOY_DIR="${DEPLOY_DIR:-/home/azureuser/ulop-deploy}"
IMAGE_REPO="${IMAGE_REPO:-ghcr.io/ase-group-10/intelligenturbanplatform-backend}"
IMAGE_TAG="${IMAGE_TAG:-}"
EXPECTED_MIGRATIONS="${EXPECTED_MIGRATIONS:-}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
SERVICES=(api c04-publisher c05-publisher c06-publisher c07-worker c08-worker c11-worker c12-worker)
REQUIRED_QUEUES=(c07.city-state-updated c08.plan-ready c11.delivery-observations c12.delivery-recordings)
MANAGED_OVERRIDE_HEADER="# Managed by scripts/azure/deploy-backend.sh."

if [[ "${EXPECTED_MIGRATIONS}" == "none" ]]; then
  EXPECTED_MIGRATIONS=""
fi

if [[ ! "${IMAGE_TAG}" =~ ^sha-[0-9a-f]{40}$ ]]; then
  echo "ERROR: IMAGE_TAG must match sha-<40-char lowercase hex commit>; got '${IMAGE_TAG}'" >&2
  exit 1
fi

if [[ ! -f "${DEPLOY_DIR}/compose.yaml" ]]; then
  echo "ERROR: ${DEPLOY_DIR}/compose.yaml not found" >&2
  exit 1
fi

if [[ ! -f "${DEPLOY_DIR}/backend-image.override.yml" ]]; then
  echo "ERROR: ${DEPLOY_DIR}/backend-image.override.yml not found" >&2
  exit 1
fi

ensure_managed_override() {
  local override_file="${DEPLOY_DIR}/compose.override.yml"
  if [[ -f "${override_file}" ]]; then
    local first_line
    first_line="$(head -n 1 "${override_file}" 2>/dev/null || true)"
    if [[ "${first_line}" != "${MANAGED_OVERRIDE_HEADER}" ]]; then
      echo "ERROR: ${override_file} exists and is not exclusively managed by scripts/azure/deploy-backend.sh; refusing to overwrite it." >&2
      return 1
    fi
  fi
  return 0
}

ensure_managed_override

cd "${DEPLOY_DIR}"

TARGET_IMAGE="${IMAGE_REPO}:${IMAGE_TAG}"
COMPOSE_FILES=(-f compose.yaml -f backend-image.override.yml)
HEALTH_BODY=""

if ! existing_api_cids="$(docker compose -f compose.yaml ps -a -q api)"; then
  echo "ERROR: Could not list existing api containers; aborting to protect rollback." >&2
  exit 1
fi
EXISTING_API_CID="${existing_api_cids%%$'\n'*}"
PREVIOUS_IMAGE=""
if [[ -n "${EXISTING_API_CID}" ]]; then
  PREVIOUS_IMAGE="$(docker inspect --format '{{.Config.Image}}' "${EXISTING_API_CID}" 2>/dev/null || true)"
  if [[ -z "${PREVIOUS_IMAGE}" ]]; then
    echo "ERROR: Found existing api container (${EXISTING_API_CID}) but could not read its image; aborting to protect rollback." >&2
    exit 1
  fi
elif [[ -f "${DEPLOY_DIR}/.last-deployed-image" ]]; then
  PREVIOUS_IMAGE="$(head -n 1 "${DEPLOY_DIR}/.last-deployed-image" | tr -d '[:space:]')"
  if [[ -z "${PREVIOUS_IMAGE}" ]]; then
    echo "ERROR: ${DEPLOY_DIR}/.last-deployed-image exists but is empty; aborting to protect rollback." >&2
    exit 1
  fi
fi

write_compose_override() {
  local image="$1"
  local override_file="${DEPLOY_DIR}/compose.override.yml"
  local tmp_file="${override_file}.tmp"
  ensure_managed_override || return 1
  if ! cat > "${tmp_file}" <<EOF
${MANAGED_OVERRIDE_HEADER}
# Loaded automatically by \`docker compose up -d\` so plain Compose commands
# stay pinned to the active verified release image.
services:
  api:
    image: ${image}
  c04-publisher:
    image: ${image}
  c05-publisher:
    image: ${image}
  c06-publisher:
    image: ${image}
  c07-worker:
    image: ${image}
  c08-worker:
    image: ${image}
  c11-worker:
    image: ${image}
  c12-worker:
    image: ${image}
EOF
  then
    rm -f "${tmp_file}"
    return 1
  fi
  if ! mv "${tmp_file}" "${override_file}"; then
    rm -f "${tmp_file}"
    return 1
  fi
  return 0
}

persist_active_image() {
  local image="$1"
  local state_file="${DEPLOY_DIR}/.last-deployed-image"
  local tmp_state="${state_file}.tmp"
  write_compose_override "${image}" || return 1
  if ! printf '%s\n' "${image}" > "${tmp_state}"; then
    rm -f "${tmp_state}"
    return 1
  fi
  if ! mv "${tmp_state}" "${state_file}"; then
    rm -f "${tmp_state}"
    return 1
  fi
  return 0
}

verify_rollout() {
  local label="$1"
  local expected_image="$2"
  echo "Verifying ${label} (${HEALTH_URL})..."

  local health_ok=0
  local body=""
  for _ in $(seq 1 15); do
    if body="$(curl -fsS --connect-timeout 2 --max-time 5 "${HEALTH_URL}" 2>/dev/null)"; then
      if [[ "${body}" == *'"status":"ok"'* && "${body}" == *'"database":"connected"'* ]]; then
        health_ok=1
        HEALTH_BODY="${body}"
        break
      fi
    fi
    sleep 2
  done

  if [[ "${health_ok}" -ne 1 ]]; then
    echo "ERROR: ${label} failed health check at ${HEALTH_URL} (last body: '${body}')" >&2
    docker compose ps || true
    return 1
  fi

  # Wait for workers to connect to RabbitMQ/Postgres and verify queues have active consumers.
  local queues_ok=0
  local queue_table=""
  for _ in $(seq 1 10); do
    queue_table="$(docker compose exec -T rabbitmq rabbitmqctl list_queues --no-table-headers name consumers 2>/dev/null || true)"
    local all_consuming=1
    for q in "${REQUIRED_QUEUES[@]}"; do
      local count
      count="$(awk -v q="${q}" '$1 == q { print $2 }' <<< "${queue_table}")"
      if [[ -z "${count}" || ! "${count}" =~ ^[0-9]+$ || "${count}" -lt 1 ]]; then
        all_consuming=0
        break
      fi
    done
    if [[ "${all_consuming}" -eq 1 ]]; then
      queues_ok=1
      break
    fi
    sleep 2
  done

  if [[ "${queues_ok}" -ne 1 ]]; then
    echo "ERROR: ${label} workers are not actively consuming required RabbitMQ queues (${REQUIRED_QUEUES[*]})" >&2
    echo "Observed queues:" >&2
    printf '%s\n' "${queue_table}" >&2
    return 1
  fi

  # Confirm every backend service stayed running with zero restarts.
  for svc in "${SERVICES[@]}"; do
    local cid status restarting restarts actual_image
    cid="$(docker compose ps -q "${svc}")"
    if [[ -z "${cid}" ]]; then
      echo "ERROR: ${label} container for '${svc}' is missing" >&2
      return 1
    fi
    status="$(docker inspect --format '{{.State.Status}}' "${cid}" 2>/dev/null || echo missing)"
    restarting="$(docker inspect --format '{{.State.Restarting}}' "${cid}" 2>/dev/null || echo true)"
    restarts="$(docker inspect --format '{{.RestartCount}}' "${cid}" 2>/dev/null || echo 1)"
    actual_image="$(docker inspect --format '{{.Config.Image}}' "${cid}" 2>/dev/null || true)"
    if [[ "${status}" != "running" || "${restarting}" != "false" || "${restarts}" != "0" ]]; then
      echo "ERROR: ${label} service '${svc}' unhealthy (status=${status}, restarting=${restarting}, restarts=${restarts})" >&2
      docker compose logs --tail=30 "${svc}" || true
      return 1
    fi
    if [[ "${actual_image}" != "${expected_image}" ]]; then
      echo "ERROR: ${label} service '${svc}' uses image '${actual_image:-unknown}', expected '${expected_image}'" >&2
      return 1
    fi
  done

  return 0
}

verify_resolved_images() {
  local expected_image="$1"
  local resolved
  if ! resolved="$(BACKEND_IMAGE="${expected_image}" docker compose "${COMPOSE_FILES[@]}" config --format json)"; then
    echo "ERROR: Could not resolve deployment Compose configuration" >&2
    return 1
  fi
  RESOLVED_COMPOSE="${resolved}" python3 - "${expected_image}" "${SERVICES[@]}" <<'PYTHON'
import json, os, sys
services = json.loads(os.environ["RESOLVED_COMPOSE"])["services"]
for name in sys.argv[2:]:
    actual = services.get(name, {}).get("image")
    if actual != sys.argv[1]:
        print(f"ERROR: Resolved image for {name}: {actual!r}; expected {sys.argv[1]}", file=sys.stderr)
        sys.exit(1)
PYTHON
}

rollback_and_verify() {
  if [[ -z "${PREVIOUS_IMAGE}" || "${PREVIOUS_IMAGE}" == "${TARGET_IMAGE}" ]]; then
    echo "ROLLBACK_STATUS=SKIPPED (no distinct previous image recorded)" >&2
    return 1
  fi

  echo "Rolling back all backend services to ${PREVIOUS_IMAGE}..." >&2
  verify_resolved_images "${PREVIOUS_IMAGE}" || return 1
  if ! BACKEND_IMAGE="${PREVIOUS_IMAGE}" docker compose "${COMPOSE_FILES[@]}" up -d "${SERVICES[@]}"; then
    echo "ROLLBACK_STATUS=FAILED (docker compose up failed while restoring ${PREVIOUS_IMAGE})" >&2
    return 1
  fi

  if ! verify_rollout "rollback (${PREVIOUS_IMAGE})" "${PREVIOUS_IMAGE}"; then
    echo "ROLLBACK_STATUS=FAILED (restored services did not pass health/worker verification)" >&2
    return 1
  fi

  if ! persist_active_image "${PREVIOUS_IMAGE}"; then
    echo "ROLLBACK_STATUS=FAILED (services restored to ${PREVIOUS_IMAGE}, but failed to persist compose.override.yml or .last-deployed-image)" >&2
    return 1
  fi
  echo "ROLLBACK_STATUS=RECOVERED (${PREVIOUS_IMAGE})" >&2
  return 0
}

echo "Target image: ${TARGET_IMAGE}"
if [[ -n "${PREVIOUS_IMAGE}" ]]; then
  echo "Previous api image: ${PREVIOUS_IMAGE}"
fi

verify_resolved_images "${TARGET_IMAGE}"

echo "Pulling target backend image..."
BACKEND_IMAGE="${TARGET_IMAGE}" docker compose "${COMPOSE_FILES[@]}" pull "${SERVICES[@]}"

echo "Checking worker database connectivity and schema_migrations before rollout..."
BACKEND_IMAGE="${TARGET_IMAGE}" EXPECTED_MIGRATIONS="${EXPECTED_MIGRATIONS}" \
  docker compose "${COMPOSE_FILES[@]}" run --rm --no-deps --entrypoint python \
  -e EXPECTED_MIGRATIONS \
  c06-publisher -c '
import os, sys, psycopg
expected = [v.strip() for v in os.environ.get("EXPECTED_MIGRATIONS", "").split(",") if v.strip()]
with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=10) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        if expected:
            cur.execute("SELECT version FROM public.schema_migrations")
            applied = {str(row[0]) for row in cur.fetchall()}
            missing = [v for v in expected if v not in applied]
            if missing:
                print("ERROR: Missing database migrations in public.schema_migrations: " + ", ".join(missing), file=sys.stderr)
                sys.exit(1)
            print(f"Worker DB reachable; all {len(expected)} expected migrations are applied.")
        else:
            print("Worker DB reachable (migration check skipped).")
'

echo "Starting updated backend services..."
if ! BACKEND_IMAGE="${TARGET_IMAGE}" docker compose "${COMPOSE_FILES[@]}" up -d "${SERVICES[@]}"; then
  echo "ERROR: docker compose up failed for ${TARGET_IMAGE}" >&2
  rollback_and_verify || true
  exit 1
fi

if ! verify_rollout "deployment (${TARGET_IMAGE})" "${TARGET_IMAGE}"; then
  rollback_and_verify || true
  exit 1
fi

persist_active_image "${TARGET_IMAGE}"
echo "Health check response: ${HEALTH_BODY}"
docker compose ps
echo "DEPLOY_RESULT=SUCCESS"
