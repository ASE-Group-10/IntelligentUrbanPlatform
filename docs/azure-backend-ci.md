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

## Deployment still to be integrated

Before adding a one-click VM deployment workflow, agree with the #75 pair and
reviewers on the deployment identity and network path. The VM currently accepts
SSH only from the operator's IP, so a GitHub-hosted runner cannot SSH into it.
Do not broaden SSH access to the entire Internet. Choose either a restricted
outbound polling mechanism from the VM or Azure's authenticated Run Command
with a narrowly scoped federated GitHub identity. Document the chosen method,
the exact Compose revision, health check, and rollback procedure before
enabling the manual deployment button.

The first deployment must be run manually and checked against `/health`, the
expected API routes, C06 publisher logs, and C07 worker logs. Database schema
migrations remain the database owner's task and are not performed by the image
pipeline.
