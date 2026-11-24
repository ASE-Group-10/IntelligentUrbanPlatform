"""Read committed generation, confirmation and revision evidence for one Dublin plan."""

import argparse
import json
import sys
from datetime import UTC, datetime
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import UUID4, TypeAdapter, ValidationError

from app.components.c04_delivery_management.planning import DeliveryLinkReceipt
from app.components.c05_planning.models import Plan, SavedDelivery
from app.components.c05_planning.store import scope_key
from app.components.c06_city_monitoring.router import libpq_url
from app.components.c07_disruption_detection.contracts import (
    Disruption,
    ReplanOutcome,
    ReplanTrigger,
)
from app.components.c08_allocation.events import PlanReadyEvent
from app.components.c10_decision_records.contracts import DecisionRecord
from app.config import get_settings
from app.demo.c05_confirmation import LOCAL_DEMO_OPERATOR_ID
from app.demo.feed import load_scenario


class _MissingEvidenceError(Exception):
    """A committed record is absent or does not substantiate this phase."""


class _PublicationPendingError(Exception):
    """The matching outbox entry exists, but the publisher has not marked it."""


def _require(condition, detail: str) -> None:
    if not condition:
        raise _MissingEvidenceError(detail)


def _plan(rows: dict, version: int) -> Plan:
    _require(version in rows, f"No plan v{version} was saved for this run and trace.")
    return Plan.model_validate(rows[version]["proposal"], strict=False)


def _decision(
    row: dict, plan: Plan, kind: str, previous: UUID | None
) -> DecisionRecord:
    _require(row["record"], f"C10 has no decision for plan v{plan.version}.")
    decision = DecisionRecord.model_validate(row["record"])
    did = plan.request_ids[0]
    inputs = decision.input_versions
    _require(
        decision.producer == "C05"
        and decision.decision_id == plan.decision_id
        and decision.decision_type == kind
        and decision.subject.type == "Plan"
        and decision.subject.id == plan.plan_id
        and decision.subject.version == plan.version
        and decision.attempt_outcome == "Selected"
        and decision.related_decision_id == previous
        and decision.run_scope.mode == "Simulation"
        and decision.run_scope.run_id == plan.run_scope.run_id
        and [(d.id, d.version) for d in inputs.deliveries]
        == [(did, plan.input_versions.get(f"delivery:{did}"))]
        and inputs.corpus_version == plan.input_versions.get("corpus")
        and decision.selected_option.details.get("segmentKeys")
        == plan.routes[0].segment_keys
        and TypeAdapter(datetime).validate_python(
            decision.selected_option.details.get("plannedDepartureAt")
        )
        == row["planned_departure_at"],
        f"The C10 {kind} decision does not match the plan, run or input versions.",
    )
    _require(
        decision.evidence_snapshots
        and all(
            s.query.vehicle_class == "van" and s.query.activity == "Delivery"
            for s in decision.evidence_snapshots
        ),
        "The C10 decision has no matching C09 delivery-policy evidence.",
    )
    return decision


def _event(row: dict, plan: Plan, trace: UUID, cause: UUID | None) -> dict:
    _require(row["event_id"], f"No PlanReady outbox row for v{plan.version}.")
    event = PlanReadyEvent.model_validate_json(json.dumps(row["envelope"]))
    segments = list(dict.fromkeys(s for r in plan.routes for s in r.segment_keys))
    _require(
        event.event_id == row["event_id"]
        and event.correlation_id == trace
        and event.scope == plan.run_scope.scope
        and event.run_id == plan.run_scope.run_id
        and event.causation_id == cause
        and event.payload.plan_id == plan.plan_id
        and event.payload.plan_version == plan.version
        and event.payload.decision_id == plan.decision_id
        and event.payload.requirements == plan.resource_needs
        and event.affected.segment_keys == segments,
        f"Published v{plan.version} does not match the saved plan or trace.",
    )
    if row["published_at"] is None:
        raise _PublicationPendingError(
            f"The v{plan.version} publication mark is pending."
        )
    return {
        "eventId": str(event.event_id),
        # Use the database clock, rather than the simulation clock, for broker logs.
        "eventTime": row["created_at"].astimezone(UTC).isoformat(),
    }


def _ok(plan: Plan, detail: str, **fields) -> dict:
    return {
        "ok": True,
        "detail": detail,
        "version": plan.version,
        "deliveryId": str(plan.request_ids[0]),
        "decisionId": str(plan.decision_id),
        **fields,
    }


def _generation(cur, rows: dict, scoped: str, run_id: UUID, plan_id: UUID):
    proposal = _plan(rows, 1)
    scenario = load_scenario()
    _require(
        proposal.status == "awaiting_confirmation"
        and len(proposal.request_ids) == 1
        and len(proposal.routes) == 1
        and proposal.routes[0].segment_keys == [scenario.closure.segment_key]
        and proposal.routes[0].delivery_ids == proposal.request_ids
        and rows[1]["event_id"] is None,
        "Expected one pending Eden Quay proposal with no v1 PlanReady.",
    )
    did = proposal.request_ids[0]
    cur.execute(
        """select g.request_body, d.delivery, d.plan_id as linked_plan
             from c05_planning.generation_receipts g
             join c04_delivery_management.deliveries d
               on d.run_id = %s and d.delivery_id = %s
            where g.scope_key = %s and g.plan_id = %s and g.version = 1
              and g.principal_id = 'service:c04'""",
        (run_id, did, scoped, plan_id),
    )
    link = cur.fetchone()
    _require(link, "Missing the generation receipt or its saved C04 delivery.")
    source = SavedDelivery.model_validate(link["request_body"])
    current = SavedDelivery.model_validate(link["delivery"])
    needs = proposal.resource_needs
    _require(
        did == source.delivery_id == current.delivery_id
        and source.status == "pending"
        and source.progress.status == "pending"
        and source.version == proposal.input_versions.get(f"delivery:{did}")
        and "van" in source.constraints.vehicle_kinds
        and len(needs) == 1
        and needs[0].delivery_id == did
        and needs[0].resource_kind == "van"
        and needs[0].capacity.unit == "kg"
        and needs[0].capacity.amount == source.cargo.weight_kg
        and current.version >= source.version
        and (current.version != source.version or current == source),
        "The saved C04 delivery and C05 generation input do not match.",
    )
    if link["linked_plan"] == plan_id and current.version == source.version + 1:
        # At confirmation C04 changes only status and version among planning facts.
        _require(
            current
            == source.model_copy(
                update={"version": current.version, "status": "planned"}
            ),
            "C04's confirmed delivery differs from the saved generation facts.",
        )
    generated = _decision(rows[1], proposal, "PlanGenerated", None)
    _require(
        all(
            s.query.time_window.start == source.time_window.start
            and s.query.time_window.end == source.time_window.end
            for s in generated.evidence_snapshots
        ),
        "The saved policy evidence is for a different delivery window.",
    )
    result = _ok(
        proposal,
        "Saved C04 delivery, proposal and C10 decision with C09 evidence.",
        evidenceSnapshotIds=[str(s.snapshot_id) for s in generated.evidence_snapshots],
    )
    return result, proposal, current, link["linked_plan"]


def _confirmation(
    cur,
    rows: dict,
    scoped: str,
    correlation_id: UUID,
    proposal: Plan,
    current: SavedDelivery,
    linked_plan: UUID | None,
):
    confirmed = _plan(rows, 2)
    initial_version = proposal.input_versions[f"delivery:{current.delivery_id}"]
    _require(
        confirmed == proposal.model_copy(update={"status": "confirmed", "version": 2})
        and linked_plan == proposal.plan_id
        and current.version > initial_version
        and rows[1]["planned_departure_at"] == rows[2]["planned_departure_at"],
        "The confirmed plan or C04 link does not match the proposal.",
    )
    cur.execute(
        """select r.receipt, c.principal_id from c05_planning.confirmation_receipts c
             join c04_delivery_management.planning_receipts r
               on r.run_id = %s and r.principal_id = 'service:c05'
              and r.idempotency_key = %s
            where c.scope_key = %s and c.plan_id = %s
              and c.expected_version = 1 and c.version = 2""",
        (
            proposal.run_scope.run_id,
            f"c05:confirm:{proposal.plan_id}:v1",
            scoped,
            proposal.plan_id,
        ),
    )
    row = cur.fetchone()
    _require(row, "Missing the operator confirmation or C04 delivery-link receipt.")
    receipt = DeliveryLinkReceipt.model_validate(row["receipt"])
    _require(
        receipt.plan_id == proposal.plan_id
        and receipt.request_ids == proposal.request_ids
        and receipt.versions == {current.delivery_id: initial_version + 1},
        "The C04 receipt does not link this delivery and version to the plan.",
    )
    result = _ok(
        confirmed,
        "Operator confirmation, C04 link and first published PlanReady.",
        **_event(rows[2], confirmed, correlation_id, None),
    )
    if row["principal_id"] == LOCAL_DEMO_OPERATOR_ID:
        result["localFixture"] = True
        result["detail"] = "Local CLI fixture confirmation and C04 delivery link."
    return result, confirmed


def _revision(
    cur,
    rows: dict,
    scoped: str,
    correlation_id: UUID,
    confirmed: Plan,
    closure_event_id: UUID | None,
):
    _require(
        closure_event_id,
        "Supply the current closure event ID to verify its revision.",
    )
    revised = _plan(rows, 3)
    detours = [
        s.segment_key
        for s in load_scenario().segments
        if s.role == "detour after the closure"
    ]
    _require(
        revised.status == "confirmed"
        and revised.request_ids == confirmed.request_ids
        and len(revised.routes) == len(detours) == 1
        and revised.routes[0].segment_keys == detours
        and revised.routes[0].delivery_ids == confirmed.request_ids,
        "The revision does not take the Dublin detour for the same delivery.",
    )
    decision = _decision(rows[3], revised, "Replan", confirmed.decision_id)
    cur.execute(
        """select r.trigger, r.outcome, c.published_at, d.disruption
             from c05_planning.replan_receipts r
             join c07_disruption_detection.disruptions d
               on d.scope_key = r.scope_key and d.disruption_id = r.disruption_id
             join c06_city_monitoring.outbox c on c.scope_key = r.scope_key
              and c.event_id = %s and c.event_type = 'CityStateUpdated'
              and c.envelope->>'eventId' = c.event_id::text
              and c.envelope->>'correlationId' = r.correlation_id::text
              and c.envelope->>'scope' = 'simulation' and c.envelope->>'runId' = %s
              and c.envelope->'payload'->'changes' @> %s
            where r.scope_key = %s and r.plan_id = %s and r.base_version = 2
              and r.correlation_id = %s and r.event_id = %s""",
        (
            closure_event_id,
            str(confirmed.run_scope.run_id),
            Jsonb(
                [
                    {
                        "segmentKey": confirmed.routes[0].segment_keys[0],
                        "status": "closed",
                    }
                ]
            ),
            scoped,
            confirmed.plan_id,
            correlation_id,
            rows[3]["event_id"],
        ),
    )
    receipt = cur.fetchone()
    _require(
        receipt, "Missing the matching C06 closure, C07 disruption or C05 receipt."
    )
    outcome = ReplanOutcome.model_validate(receipt["outcome"])
    trigger = ReplanTrigger.model_validate(receipt["trigger"])
    _require(
        outcome.status == "PlanSaved"
        and outcome.new_plan_version == revised.version
        and outcome.decision_id == revised.decision_id
        and trigger.source_event_id == closure_event_id
        and trigger.based_on_plan_version == confirmed.version
        and trigger.reason == "road_closed"
        and decision.selected_option.details.get("trigger") == receipt["trigger"]
        and decision.input_versions.city_snapshot_version
        == revised.input_versions.get("cityState"),
        "Replan receipt or C10 decision does not match this closure and revision.",
    )
    if receipt["published_at"] is None:
        raise _PublicationPendingError("The C06 closure publication mark is pending.")
    disruption = Disruption.model_validate(receipt["disruption"])
    _require(
        closure_event_id in disruption.trigger_event_ids
        and disruption.disruption_id == trigger.disruption_id
        and disruption.version == trigger.disruption_version,
        "The C07 disruption does not identify this closure and trigger.",
    )
    affected = next(
        (p for p in disruption.affected_plans if p.plan_id == revised.plan_id), None
    )
    _require(
        affected is not None, "C07 did not record this plan among the affected plans."
    )
    if not affected.replan_requested:
        # C07 persists the response after C05 commits. The event can arrive first.
        return {
            "ok": False,
            "pendingOutcome": True,
            "detail": "The C07 replan outcome is pending.",
        }
    _require(
        affected.plan_version == trigger.based_on_plan_version
        and affected.replan_outcome == "PlanSaved",
        "C07 did not record PlanSaved for this plan and base version.",
    )
    return _ok(
        revised,
        "C07 recorded PlanSaved for the detour and C05 published its second PlanReady.",
        evidenceSnapshotIds=[str(s.snapshot_id) for s in decision.evidence_snapshots],
        **_event(rows[3], revised, correlation_id, closure_event_id),
    )


def collect_evidence(
    conn: psycopg.Connection,
    *,
    run_id: UUID,
    correlation_id: UUID,
    plan_id: UUID,
    closure_event_id: UUID | None = None,
) -> dict:
    """Verify persisted simulation evidence. The caller supplies the read snapshot."""
    scoped = scope_key("simulation", run_id)
    report = {
        "runId": str(run_id),
        "correlationId": str(correlation_id),
        "planId": str(plan_id),
        "closureEventId": str(closure_event_id) if closure_event_id else None,
        "origin": "saved-delivery",
        "generation": {"ok": False, "detail": "No generation evidence."},
        "confirmation": {"ok": False, "detail": "Generation was not verified."},
        "revision": {"ok": False, "detail": "Confirmation was not verified."},
    }
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """select v.version, v.proposal, v.planned_departure_at, d.record,
                      o.event_id, o.envelope, o.created_at, o.published_at
                 from c05_planning.plan_versions v
                 left join c10_decision_records.decisions d
                   on d.decision_id = v.decision_id
                 left join c05_planning.outbox o
                   on (o.scope_key, o.plan_id, o.version) =
                      (v.scope_key, v.plan_id, v.version) and o.event_type = 'PlanReady'
                where v.scope_key = %s and v.plan_id = %s and v.correlation_id = %s
                  and v.version between 1 and 3""",
            (scoped, plan_id, correlation_id),
        )
        rows = {row["version"]: row for row in cur}
        phase = "generation"
        try:
            report[phase], proposal, current, linked_plan = _generation(
                cur, rows, scoped, run_id, plan_id
            )
            phase = "confirmation"
            report[phase], confirmed = _confirmation(
                cur, rows, scoped, correlation_id, proposal, current, linked_plan
            )
            phase = "revision"
            report[phase] = _revision(
                cur, rows, scoped, correlation_id, confirmed, closure_event_id
            )
        except _PublicationPendingError as exc:
            report[phase] = {
                "ok": False,
                "pendingPublication": True,
                "detail": str(exc),
            }
        except _MissingEvidenceError as exc:
            report[phase] = {"ok": False, "detail": str(exc)}
        except ValidationError:
            report[phase] = {
                "ok": False,
                "detail": "Stored evidence failed contract validation.",
            }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("--run-id", "--correlation-id", "--plan-id", "--closure-event-id"):
        parser.add_argument(
            flag,
            required=flag != "--closure-event-id",
            type=TypeAdapter(UUID4).validate_python,
        )
    args = parser.parse_args()
    try:
        dsn = libpq_url(get_settings().database_url.get_secret_value())
        with psycopg.connect(dsn) as conn:
            conn.execute("set transaction isolation level repeatable read, read only")
            report = collect_evidence(conn, **vars(args))
    except (psycopg.Error, ValueError):
        print("Unable to read the local C05 verification database.", file=sys.stderr)
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
