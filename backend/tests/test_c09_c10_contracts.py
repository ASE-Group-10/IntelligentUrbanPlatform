"""Every example in contracts/c09_c10/examples must validate against its model.
Test doubles return these same files, so a failing test means the contract drifted."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from app.components.c09_logistics_knowledge import contracts as c09
from app.components.c10_decision_records import contracts as c10
from app.shared.c09_c10_common import ErrorBody

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "c09_c10" / "examples"

MODELS = {
    "query_knowledge.request.json": c09.PolicyQuery,
    "query_knowledge.ok.response.json": c09.KnowledgeResult,
    "query_knowledge.low_confidence.response.json": c09.KnowledgeResult,
    "query_knowledge.conflict.response.json": c09.KnowledgeResult,
    "error.unavailable.json": ErrorBody,
    "publish_knowledge.request.json": c09.PublishKnowledgeRequest,
    "publish_knowledge.response.json": c09.KnowledgePublication,
    "record_outcome.request.json": c10.AuditOutcome,
    "record_outcome.response.json": c10.AuditId,
    "record_decision.request.json": c10.DecisionRecord,
    "record_decision.response.json": c10.RecordDecisionResponse,
    "explain_decision.request.json": c10.ExplainRequest,
    "explain_decision.ok.response.json": c10.Explanation,
    "explain_decision.partial.response.json": c10.Explanation,
    "get_decision_evidence.response.json": c10.DecisionEvidence,
    "error.decision_ambiguous.json": ErrorBody,
}


def load(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def test_every_example_has_a_model():
    assert {p.name for p in EXAMPLES.glob("*.json")} == set(MODELS)


@pytest.mark.parametrize("name", sorted(MODELS))
def test_example_validates_and_round_trips(name):
    data = load(name)
    obj = TypeAdapter(MODELS[name]).validate_python(data)
    # dumping by alias must give back the same JSON, so field names stay camelCase
    assert json.loads(
        TypeAdapter(MODELS[name]).dump_json(obj, by_alias=True)
    ) == json.loads(json.dumps(data))


def test_snapshot_from_query_is_accepted_by_record_decision():
    """The seam between the two halves: C09's snapshot goes into C10 unchanged."""
    snap = load("query_knowledge.ok.response.json")["evidenceSnapshot"]
    assert load("record_decision.request.json")["evidenceSnapshots"] == [snap]


def bad(name, mutate, model):
    data = copy.deepcopy(load(name))
    mutate(data)
    with pytest.raises(ValidationError):
        TypeAdapter(model).validate_python(data)


def test_rejects_reason_citing_unknown_evidence():
    bad(
        "record_decision.request.json",
        lambda d: d["reasons"][0]["evidenceRefIds"].append(
            "00000000-0000-4000-8000-000000000000"
        ),
        c10.DecisionRecord,
    )


def test_rejects_evidence_without_content():
    bad(
        "record_decision.request.json",
        lambda d: d["evidenceSnapshots"][0]["items"][0].update(content=""),
        c10.DecisionRecord,
    )


def test_rejects_unknown_field():
    bad("query_knowledge.request.json", lambda d: d.update(radiusKm=3), c09.PolicyQuery)


def test_rejects_reversed_time_window():
    bad(
        "query_knowledge.request.json",
        lambda d: d["timeWindow"].update(end="2026-11-20T07:00:00Z"),
        c09.PolicyQuery,
    )


def test_rejects_conflict_status_without_conflicts():
    def m(d):
        d["conflicts"] = []
        d["evidenceSnapshot"]["conflicts"] = []

    bad("query_knowledge.conflict.response.json", m, c09.KnowledgeResult)


def test_rejects_partial_explanation_without_gaps():
    bad(
        "explain_decision.partial.response.json",
        lambda d: d.update(gaps=[]),
        c10.Explanation,
    )


def test_rejects_simulation_without_run_id():
    bad(
        "record_decision.request.json",
        lambda d: d["runScope"].update(mode="Simulation"),
        c10.DecisionRecord,
    )
