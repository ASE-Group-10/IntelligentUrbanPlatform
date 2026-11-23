"""Who may read a simulation run's records over HTTP (C06, C07).

shortcut: no component records who owns a simulation run yet (C12 internal.md
lists the owner as pending), so only runs configured as shared, the thin-slice
demo run by default (``SHARED_RUN_IDS``), can be read. Live state is open to
every signed-in user.
"""

from uuid import UUID

from fastapi import Request

from app.infrastructure.errors import APIError


def require_run_access(request: Request, run_id: UUID | None) -> None:
    """Raise ``403 Forbidden`` unless ``run_id`` is live (``None``) or shared."""
    if run_id is not None and run_id not in request.app.state.settings.shared_run_ids:
        raise APIError(403, "Forbidden", "The caller may not read this simulation run.")
