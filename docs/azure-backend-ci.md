# Azure development backend image pipeline (#75)

## Dependency on local Compose setup

Merge PR #79 before this pipeline. PR #79 owns `backend/Dockerfile` and the
local Compose services. This PR reuses that Dockerfile to publish the same
backend image to GitHub Container Registry; it does not define another image.
Do not merge this PR into `develop` while `backend/Dockerfile` is absent.

## Scope and gate

The `Backend checks` workflow tests and lints backend changes on `develop`.
After the checks succeed on a push, its `Publish backend image` job builds that
exact revision for `linux/amd64` and publishes it to GitHub Container Registry
as `ghcr.io/ase-group-10/intelligenturbanplatform-backend:sha-<commit>`.

Publishing an image does **not** update the VM. The development VM remains on
its previous image until the operator confirms that database-owner migrations,
runtime environment variables, and component compatibility are ready. This
gate is deliberate while the shared database and workers evolve independently.

No database URL, RabbitMQ password, or SSH private key belongs in this image or
repository. Keep those runtime values in the VM's restricted `.env` file.

## Verify a published image

1. Open GitHub Actions → `Backend checks` for the `develop` commit and confirm
   both jobs succeeded. Note the full `sha-<commit>` tag; do not use a floating
   `latest` tag.
2. Confirm that the repository's GitHub package permissions allow the VM's
   read-only package credential to pull the image. Do not make the package
   public merely to simplify deployment.

## Manual VM deployment workflow (CD)

Deployment to `vm-ulop-rabbitmq-dev` (resource group `rg-ulop-dev`) is
triggered manually via the **`Deploy Azure backend`** GitHub Actions workflow
(`.github/workflows/deploy-azure-backend.yml`). It never runs automatically on
push to `develop`, so database migrations and runtime configuration remain gated
by the operator.

### Authentication and network path

- SSH (port 22) remains restricted to the operator's IP; GitHub-hosted runners
  never SSH into the VM.
- GitHub Actions authenticates to Azure using OIDC federated credentials bound
  to `ASE-Group-10/IntelligentUrbanPlatform` (`develop` branch) on the
  user-assigned managed identity `id-ulop-github-deploy`, scoped to
  `Virtual Machine Contributor` on `vm-ulop-rabbitmq-dev`.
- Required repository secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, and
  `AZURE_SUBSCRIPTION_ID`.
- The workflow invokes `scripts/azure/deploy-backend.sh` on the VM through
  `az vm run-command invoke`.

### VM Compose layout

The VM runs from `/home/azureuser/ulop-deploy` using its host-local
`compose.yaml` and `.env`. During a release, `scripts/azure/deploy-backend.sh`
applies `scripts/azure/backend-image.override.yml` to pin the eight backend
services (`api`, `c04-publisher`, `c05-publisher`, `c06-publisher`,
`c07-worker`, `c08-worker`, `c11-worker`, `c12-worker`) to
`${BACKEND_IMAGE}`. Once verification succeeds (or after a verified rollback),
the script also writes the active image into
`/home/azureuser/ulop-deploy/compose.override.yml` so subsequent plain
`docker compose up -d` commands keep running the deployed revision instead of
reverting to `compose.yaml`'s default image.

### Deployment, verification, and rollback

1. Confirm that `Backend checks` succeeded for the target `develop` commit and
   that the database owner has applied any new migrations in
   `database/migrations/`.
2. Open GitHub Actions → `Deploy Azure backend` → **Run workflow** on `develop`.
   Leave `image_tag` empty to deploy the checked-out commit, or enter a specific
   `sha-<40-char commit>` tag.
3. `scripts/azure/deploy-backend.sh` performs:
   - Tag format validation (`sha-<40-char hex>`, rejecting floating tags).
   - Previous-image discovery via `docker compose -f compose.yaml ps -a -q api`
     (falling back to `.last-deployed-image` if no container exists yet; aborts
     before touching containers if an existing deployment's image cannot be
     determined).
   - Pulling the target GHCR image using the VM's read-only Docker credentials.
   - Pre-deploy worker DB connectivity and migration check comparing
     `database/migrations/*.sql` from the **target commit** (`image_tag`)
     against `public.schema_migrations` (aborts before recreating containers if
     any migration is missing).
   - `docker compose -f compose.yaml -f backend-image.override.yml up -d` for
     all eight backend services listed above (wrapped so partial
     rollout failures trigger rollback instead of leaving mixed versions).
   - Bounded-timeout health check (`--connect-timeout 2 --max-time 5`) against
     `http://127.0.0.1:8000/health` checking `"status":"ok"` and
     `"database":"connected"`, RabbitMQ active-consumer check on
     `c07.city-state-updated`, `c08.plan-ready`, `c11.delivery-observations`,
     and `c12.delivery-recordings`, and verification that all
     eight services stay `running` with zero restarts.
   - Persisting the verified image to `compose.override.yml` and
     `.last-deployed-image`.
4. **Rollback**:
   - **Automatic**: If `docker compose up`, `/health`, RabbitMQ consumer
     attachment, or container restart checks fail, the script restores all eight
     services to the previous `api` image, re-runs the full health and worker
     verification, updates `compose.override.yml`, and reports
     `ROLLBACK_STATUS=RECOVERED` or `ROLLBACK_STATUS=FAILED`.
   - **Manual**: Re-run `Deploy Azure backend` with the previous verified
     `sha-<commit>` tag from the prior `Deploy Azure backend` workflow run log
     (`Previous api image:` / `Target image:`). `/home/azureuser/ulop-deploy/.last-deployed-image`
     records the currently active deployed image.

### First manual deployment evidence

The initial GHCR rollout (`sha-d39c096211d5f9e026ac36a8f74b2c77e3003bc7`) was
run manually on `vm-ulop-rabbitmq-dev` and verified against:

- `GET http://127.0.0.1:8000/health` → `200 OK` (`database: connected`)
- OpenAPI routes for C02, C03, C06, C07, and C08
- `c06-publisher`, `c07-worker` (Postgres store), and `c08-worker` running cleanly

## Public HTTPS ingress (Nginx + Let's Encrypt)

The VM's FastAPI container stays bound to `127.0.0.1:8000` (ports `8000`, `5672`,
and `15672` are never opened in the Azure NSG). External HTTPS traffic enters
through host Nginx on ports `80`/`443` for
`ulop-dev-api.swedencentral.cloudapp.azure.com`:

1. Copy `scripts/azure/nginx-ulop-api-bootstrap.conf` to
   `/etc/nginx/sites-available/ulop-api.conf`, enable it in
   `/etc/nginx/sites-enabled/ulop-api.conf`, remove
   `/etc/nginx/sites-enabled/default`, create `/var/www/html`, and reload Nginx
   (`sudo nginx -t && sudo systemctl reload nginx`). This HTTP-only config
   serves only `/.well-known/acme-challenge/`; all other requests get `404`.
2. Open inbound TCP `80` and `443` in the VM's Azure Network Security Group
   (keep `22` restricted to operator IPs), then obtain the certificate with
   `sudo certbot certonly --webroot -w /var/www/html -d ulop-dev-api.swedencentral.cloudapp.azure.com`.
   Check that both files under `/etc/letsencrypt/live/ulop-dev-api.swedencentral.cloudapp.azure.com/`
   exist before replacing the bootstrap configuration with
   `scripts/azure/nginx-ulop-api.conf`. Test and reload Nginx again. Certbot
   renewals retain the webroot challenge on port `80`.
   The final HTTPS listener rejects unknown TLS names during the handshake and
   returns `404` for a mismatched HTTP `Host` sent with the expected TLS name.
   It forwards only `/health` and `/api/*`; `/api/v1/development*`, `/`,
   `/static/*`, `/docs`, `/redoc`, and `/openapi.json` return `404`.
3. In `/home/azureuser/ulop-deploy/.env` (passed into the `api` service in
   `compose.yaml`), set:
   - `ALLOWED_HOSTS=localhost,127.0.0.1,ulop-dev-api.swedencentral.cloudapp.azure.com`
   - `CORS_ORIGINS=<https://frontend-origin>` (explicit frontend origin(s), never `*`;
     default `:443`/`:80` ports are normalized, while nondefault ports remain)
   - `SUPABASE_URL=<https://project-ref.supabase.co>`

The host Compose file must define all eight services before using this deployment
script. All eight must exist in the selected image, including rollback images;
do not roll back to revisions predating these entry points. The demo-feed
one-shot service is not part of the continuously running release group.
