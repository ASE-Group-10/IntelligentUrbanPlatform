"""Local fixture confirmation for the Dublin Compose walkthrough."""

import os
import re
from datetime import UTC, datetime
from ipaddress import ip_address
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg.conninfo import conninfo_to_dict

from app.components.c05_planning.confirmation import confirm_plan
from app.components.c05_planning.models import Plan, PlanScope
from app.components.c05_planning.store import PostgresPlanStore, scope_key
from app.components.c10_decision_records.postgres_store import PostgresDecisionStore
from app.components.c10_decision_records.service import DecisionRecords
from app.shared.auth import AuthContext
from app.shared.result import Failure, Ok, Result

# These reserved identities exist only in fixture receipts and audit records.
LOCAL_DEMO_OPERATOR_ID = uuid5(NAMESPACE_URL, "urn:ulop:local-demo:c05:operator")
LOCAL_DEMO_PROVIDER_ID = uuid5(NAMESPACE_URL, "urn:ulop:local-demo:c05:provider")
_LOCAL_ONLY = Failure(
    "Forbidden", "Fixture confirmation requires the isolated local demo database."
)


def _check_target(dsn: str) -> tuple[str, str] | Failure:
    if os.environ.get("C05_LOCAL_DEMO") != "1":
        return _LOCAL_ONLY
    # A libpq service or hostaddr may override the host in DATABASE_URL.
    if any(os.environ.get(k) for k in ("PGHOSTADDR", "PGSERVICE", "PGOPTIONS")):
        return _LOCAL_ONLY
    try:
        url = urlsplit(dsn)
        if (
            url.scheme not in {"postgres", "postgresql"}
            or url.query not in {"", "sslmode=disable"}
            or url.fragment
        ):
            return _LOCAL_ONLY
        target = conninfo_to_dict(dsn)
        host, database, user = target["host"], target["dbname"], target["user"]
        if not ip_address(host).is_loopback:
            return _LOCAL_ONLY
    except (KeyError, ValueError, psycopg.Error):
        return _LOCAL_ONLY
    if (database, user) != ("ulop", "ulop") and not re.fullmatch(
        r"[a-z0-9]+(?:_[a-z0-9]+)*_test", database
    ):
        return _LOCAL_ONLY
    return host, database


def _check_server(conn: psycopg.Connection, host: str, dbname: str) -> Failure | None:
    database, role, server, client = conn.execute(
        """select current_database(), current_user,
                  host(inet_server_addr()), host(inet_client_addr())"""
    ).fetchone()
    if (
        database != dbname
        or role != conn.info.user
        or not server
        or not client
        or ip_address(server) != ip_address(host)
        or not ip_address(client).is_loopback
    ):
        return _LOCAL_ONLY
    return None


def confirm_demo_plan(
    dsn: str,
    scope: PlanScope,
    shared_run_ids: frozenset[UUID],
    plan_id: UUID,
    trace_id: UUID | None = None,
) -> Result[tuple[Plan, UUID]]:
    """Confirm the original imported proposal under its saved simulation trace."""
    target = _check_target(dsn)
    if isinstance(target, Failure):
        return target
    if scope.scope != "simulation" or scope.run_id not in shared_run_ids:
        return Failure("Forbidden", "Choose a configured shared simulation run.")

    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            if rejected := _check_server(conn, *target):
                return rejected
            # Only the saved-demo importer populates initial_snapshot_hash.
            proposal = conn.execute(
                """select v.version, v.correlation_id
                     from c05_planning.plan_versions v
                     join c05_planning.generation_receipts g
                       using (scope_key, plan_id, version)
                     join c04_delivery_management.deliveries d
                       on d.run_id = %s and d.delivery_id =
                          (g.request_body ->> 'deliveryId')::uuid
                    where v.scope_key = %s and v.plan_id = %s and v.version = 1
                      and v.proposal ->> 'status' = 'awaiting_confirmation'
                      and g.principal_id = 'service:c04'
                      and d.initial_snapshot_hash is not null""",
                (scope.run_id, scope_key(scope.scope, scope.run_id), plan_id),
            ).fetchone()
            if proposal is None:
                return Failure("NotFound", "No imported demo proposal in this run.")
            version, original_trace = proposal
            if trace_id is not None and trace_id != original_trace:
                return Failure("Invalid", "Use the proposal's original correlation ID.")
            (collision,) = conn.execute(
                """select exists (select from c02_identity_access.profiles
                                    where principal_id = %s)
                       or exists (select from c02_identity_access.providers
                                    where provider_id = %s)""",
                (LOCAL_DEMO_OPERATOR_ID, LOCAL_DEMO_PROVIDER_ID),
            ).fetchone()
            if collision:
                return Failure("Forbidden", "A reserved fixture identity is in use.")
    except psycopg.Error:
        return Failure("Unavailable", "Could not verify the local demo database.")

    actor = AuthContext(
        principal_id=LOCAL_DEMO_OPERATOR_ID,
        principal_type="user",
        acting_provider_id=LOCAL_DEMO_PROVIDER_ID,
        roles=("operator",),
        provider_ids=(LOCAL_DEMO_PROVIDER_ID,),
        platform_roles=(),
        correlation_id=original_trace,
    )

    def clock() -> datetime:
        return datetime.now(UTC)

    # Use the same C05 entry as PlanningService and the verified database.
    result = confirm_plan(
        PostgresPlanStore(dsn),
        DecisionRecords(PostgresDecisionStore(), clock),
        clock,
        actor,
        scope,
        plan_id,
        version,
        f"confirm:{plan_id}:v{version}",
    )
    if isinstance(result, Failure):
        return result
    return Ok((result.value, original_trace))
