"""Every example in the C09/C10 openapi.yaml must validate against its model.
Test doubles return these same examples; a failing test means the contract drifted."""

import copy
import json
from pathlib import Path

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

from app.components.c09_logistics_knowledge import contracts as c09
from app.components.c10_decision_records import contracts as c10
from app.shared.c09_c10_common import ErrorBody

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts" / "components"
FILES = [
    CONTRACTS / "c09-logistics-knowledge" / "openapi.yaml",
    CONTRACTS / "c10-decision-records" / "openapi.yaml",
]
EXAMPLES = {
    name: example["value"]
    for f in FILES
    for name, example in yaml.safe_load(f.read_text(encoding="utf-8"))["components"][
        "examples"
    ].items()
}

MODELS = {
    "C09QueryKnowledgeRequest": c09.PolicyQuery,
    "C09QueryKnowledgeOkResponse": c09.KnowledgeResult,
    "C09QueryKnowledgeLowConfidenceResponse": c09.KnowledgeResult,
    "C09QueryKnowledgeConflictResponse": c09.KnowledgeResult,
    "C09ErrorUnavailable": ErrorBody,
    "C09PublishKnowledgeRequest": c09.PublishKnowledgeRequest,
    "C09PublishKnowledgeResponse": c09.KnowledgePublication,
    "C10RecordOutcomeRequest": c10.AuditOutcome,
    "C10RecordOutcomeResponse": c10.AuditId,
    "C10RecordDecisionRequest": c10.DecisionRecord,
    "C10RecordDecisionResponse": c10.RecordDecisionResponse,
    "C10ExplainDecisionRequest": c10.ExplainRequest,
    "C10ExplainDecisionOkResponse": c10.Explanation,
    "C10ExplainDecisionPartialResponse": c10.Explanation,
    "C10GetDecisionEvidenceResponse": c10.DecisionEvidence,
    "C10ErrorDecisionAmbiguous": ErrorBody,
}


def load(name):
    return copy.deepcopy(EXAMPLES[name])


def test_every_example_has_a_model():
    assert set(EXAMPLES) == set(MODELS)


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
    snap = load("C09QueryKnowledgeOkResponse")["evidenceSnapshot"]
    assert load("C10RecordDecisionRequest")["evidenceSnapshots"] == [snap]


def bad(name, mutate, model):
    data = copy.deepcopy(load(name))
    mutate(data)
    with pytest.raises(ValidationError):
        TypeAdapter(model).validate_python(data)


def test_rejects_reason_citing_unknown_evidence():
    bad(
        "C10RecordDecisionRequest",
        lambda d: d["reasons"][0]["evidenceRefIds"].append(
            "00000000-0000-4000-8000-000000000000"
        ),
        c10.DecisionRecord,
    )


def test_rejects_evidence_without_content():
    bad(
        "C10RecordDecisionRequest",
        lambda d: d["evidenceSnapshots"][0]["items"][0].update(content=""),
        c10.DecisionRecord,
    )


def test_rejects_unknown_field():
    bad("C09QueryKnowledgeRequest", lambda d: d.update(radiusKm=3), c09.PolicyQuery)


def test_rejects_reversed_time_window():
    bad(
        "C09QueryKnowledgeRequest",
        lambda d: d["timeWindow"].update(end="2026-11-20T07:00:00Z"),
        c09.PolicyQuery,
    )


def test_rejects_conflict_status_without_conflicts():
    def m(d):
        d["conflicts"] = []
        d["evidenceSnapshot"]["conflicts"] = []

    bad("C09QueryKnowledgeConflictResponse", m, c09.KnowledgeResult)


def test_rejects_partial_explanation_without_gaps():
    bad(
        "C10ExplainDecisionPartialResponse",
        lambda d: d.update(gaps=[]),
        c10.Explanation,
    )


def test_rejects_simulation_without_run_id():
    bad(
        "C10RecordDecisionRequest",
        lambda d: d["runScope"].update(mode="Simulation"),
        c10.DecisionRecord,
    )
