"""Every C09/C10 contract example (openapi.yaml and examples/*.json) must validate
against its model. A failing test means the contract drifted."""

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
# in-process examples live as files: examples/record-decision-request.json is
# loaded as "C10RecordDecisionRequest"
for openapi_file in FILES:
    for path in sorted((openapi_file.parent / "examples").glob("*.json")):
        prefix = openapi_file.parent.name[:3].upper()  # "c10-…" -> "C10"
        name = prefix + "".join(w.capitalize() for w in path.stem.split("-"))
        EXAMPLES[name] = json.loads(path.read_text(encoding="utf-8"))

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
    "C10RecordDecisionDataUnavailableRequest": c10.DecisionRecord,
    "C10GetDecisionResponse": c10.DecisionRecordView,
    "C10ExplainDecisionRequest": c10.ExplainRequest,
    "C10ExplainDecisionReplayRequest": c10.ExplainRequest,
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


def test_replay_selector_carries_the_run_id():
    """C12 selects a recorded decision by runId + decisionId (#46)."""
    req = c10.ExplainRequest.model_validate(load("C10ExplainDecisionReplayRequest"))
    assert isinstance(req.selector, c10.DecisionIdSelector)
    assert req.selector.run_id is not None
    plain = c10.ExplainRequest.model_validate(load("C10ExplainDecisionRequest"))
    assert plain.selector.run_id is None  # live callers may omit it


# ---- C05 review of #21 ----


def _constraint(**changes):
    c = load("C09QueryKnowledgeOkResponse")["passages"][0]["constraints"][0]
    c.update(changes)
    return c


def test_demo_loading_window_is_scoped_to_its_street_segment():
    windows = [
        c
        for p in load("C09QueryKnowledgeOkResponse")["passages"]
        for c in p["constraints"]
        if c["kind"] == "LoadingWindow"
    ]
    assert windows and all(c["segmentKeys"] for c in windows)


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "MaxGrossWeight", "value": None},
        {"kind": "MaxGrossWeight", "value": {"maxKg": 0}},
        {"kind": "MaxGrossWeight", "value": {"maxTonnes": 18}},
        {"kind": "LoadingWindow", "timeOfDay": None},
        {"kind": "VehicleBan", "value": {"maxKg": 18000}},
        {"segmentKeys": ["Grafton Street"]},
        {"zoneIds": []},
    ],
)
def test_rejects_constraint_missing_fields_for_its_kind(changes):
    with pytest.raises(ValidationError):
        c09.Constraint.model_validate(_constraint(**changes))


def test_accepts_max_gross_weight_with_limit():
    c09.Constraint.model_validate(
        _constraint(kind="MaxGrossWeight", timeOfDay=None, value={"maxKg": 18000})
    )


def test_rejects_result_and_snapshot_with_different_corpus_versions():
    bad(
        "C09QueryKnowledgeOkResponse",
        lambda d: d.update(corpusVersion=d["corpusVersion"] + 1),
        c09.KnowledgeResult,
    )


def test_rejects_incomplete_coverage_without_a_limitation():
    def m(d):
        for x in (d, d["evidenceSnapshot"]):
            x.update(ruleCoverage="Incomplete", limitations=[])

    bad("C09QueryKnowledgeOkResponse", m, c09.KnowledgeResult)


def test_rejects_snapshots_from_another_corpus_version():
    bad(
        "C10RecordDecisionRequest",
        lambda d: d["inputVersions"].update(corpusVersion=99),
        c10.DecisionRecord,
    )


def test_rejects_selected_decision_with_incomplete_rule_coverage():
    bad(
        "C10RecordDecisionRequest",
        lambda d: d["evidenceSnapshots"][0].update(
            ruleCoverage="Incomplete", limitations=["index rebuilding"]
        ),
        c10.DecisionRecord,
    )


def test_failed_attempt_has_no_plan_version_or_selected_option():
    record = c10.DecisionRecord.model_validate(
        load("C10RecordDecisionDataUnavailableRequest")
    )
    assert record.attempt_outcome is c10.AttemptOutcome.DATA_UNAVAILABLE
    assert record.selected_option is None and record.subject.version is None


@pytest.mark.parametrize(
    "changes",
    [
        {"selectedOption": None},  # Selected needs an option
        {"attemptOutcome": "NoChange"},  # only Selected has an option
    ],
)
def test_selected_option_matches_the_outcome(changes):
    bad("C10RecordDecisionRequest", lambda d: d.update(changes), c10.DecisionRecord)


def test_planning_attempt_cannot_carry_a_version():
    bad(
        "C10RecordDecisionDataUnavailableRequest",
        lambda d: d["subject"].update(version=1),
        c10.DecisionRecord,
    )
