"""UC10 reads over HTTP: C02 sign-in and roles, run access and C02's errors (#60)."""

import json
from contextlib import nullcontext
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.components.c02_identity_access import records
from app.components.c10_decision_records.contracts import DecisionRecord
from app.components.c10_decision_records.postgres_store import PostgresDecisionStore
from app.components.c10_decision_records.service import DecisionRecords
from app.components.c10_decision_records.store import InMemoryDecisionStore
from app.config import Settings
from app.main import create_app
from app.shared.c09_c10_common import AuthContext, ErrorResponse
from app.shared.result import Ok

CONTRACT = (
    Path(__file__).resolve().parents[2] / "contracts/components/c10-decision-records"
)
OPENAPI = yaml.safe_load((CONTRACT / "openapi.yaml").read_text(encoding="utf-8"))
RUN = UUID("c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b")
PRIVATE_RUN = UUID("d4a2b3c5-6e7f-4081-9bac-1d2e3f4a5b6c")
PASSAGE = "5b0c2f1e-8a4d-4c2b-9e61-3f7d2a9c1b01"
ROLES = ("operator", "analyst", "manager", "driver", "curator")
C05 = AuthContext(
    principal_id="service:c05",
    principal_type="Service",
    roles=["Service:C05"],
    correlation_id=uuid4(),
)


def _record(run: UUID | None = RUN, **changes) -> DecisionRecord:
    path = CONTRACT / "examples/record-decision-request.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    scope = {"mode": "Simulation" if run else "Live", "runId": run and str(run)}
    return DecisionRecord.model_validate(data | {"runScope": scope} | changes)


def _service(store) -> DecisionRecords:
    return DecisionRecords(store, clock=lambda: _record().decided_at)


class Api:
    """The app on an in-memory C10 store, called as a user with one C02 role."""

    def __init__(self, client, users, provider):
        self.client, self.users, self.provider = client, users, provider
        self.store = InMemoryDecisionStore()
        self.conn = self.store.begin()  # one open transaction stands in for a DB
        client.app.state.decisions = _service(self.store)
        client.app.state.decision_connection = lambda: nullcontext(self.conn)

    def save(self, *found: DecisionRecord) -> None:
        for record in found:
            result = self.client.app.state.decisions.record_decision(
                self.conn, C05, record
            )
            assert isinstance(result, Ok)

    def get(self, path="", role="operator", **params):
        headers = {"X-Acting-Provider": str(self.provider)}
        if role:
            headers["Authorization"] = f"Bearer {self.users[role]}"
        return self.client.get(
            f"/api/v1/decisions{path}", params=params, headers=headers
        )

    def read_all(self, record, role="operator", **params) -> list:
        """The response of each read operation for the same decision."""
        subject = {
            "subjectType": record.subject.type.value,
            "subjectId": str(record.subject.id),
        }
        return [
            self.get(f"/{record.decision_id}", role, **params),
            self.get("", role, **subject, **params),
            self.get(f"/{record.decision_id}/evidence/{PASSAGE}", role, **params),
        ]


@pytest.fixture
def api():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    ).execution_options(schema_translate_map={"c02_identity_access": None})
    records.metadata.create_all(engine)
    provider = uuid4()
    users = {role: uuid4() for role in (*ROLES, "admin")}
    with engine.begin() as conn:
        conn.execute(
            records.providers.insert(),
            {"provider_id": provider, "provider_name": "Dublin"},
        )
        for role, user in users.items():
            conn.execute(
                records.profiles.insert(),
                {
                    "principal_id": user,
                    "display_name": role,
                    "platform_admin": role == "admin",
                },
            )
        for role in ROLES:
            conn.execute(
                records.memberships.insert(),
                {"principal_id": users[role], "provider_id": provider, "role": role},
            )

    class Verifier:
        def subject(self, token):
            return UUID(token)

    settings = Settings(database_url="sqlite://", SHARED_RUN_IDS={RUN})
    try:
        with TestClient(
            create_app(engine, settings, identity_verifier=Verifier())
        ) as client:
            yield Api(client, users, provider)
    finally:
        engine.dispose()


def _assert_error(response, status, code):
    assert response.status_code == status, response.text
    ErrorResponse.model_validate(response.json())
    error = response.json()["error"]
    assert error["code"] == code
    assert error["correlation_id"] == response.headers["X-Correlation-ID"]
    return error


def _assert_read(responses, record):
    by_id, by_subject, evidence = responses
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert by_id.json() == by_subject.json()
    body = by_id.json()
    assert body.pop("recordedAt")
    assert {e["status"] for e in body.pop("evidenceStatus")} == {"Stored"}
    assert body == record.model_dump(mode="json", by_alias=True)
    example = OPENAPI["components"]["examples"]["C10GetDecisionEvidenceResponse"]
    assert evidence.json() == example["value"]


@pytest.mark.parametrize("role", ["operator", "analyst"])
def test_operator_and_analyst_read_decisions_of_a_shared_run(api, role):
    record = _record()
    api.save(record)

    _assert_read(api.read_all(record, role, runId=str(RUN)), record)


@pytest.mark.parametrize("role", ["manager", "driver", "curator", "admin"])
def test_other_roles_are_forbidden(api, role):
    record = _record()
    api.save(record)

    for response in api.read_all(record, role, runId=str(RUN)):
        _assert_error(response, 403, "C10.FORBIDDEN")


def test_sign_in_is_required(api):
    for response in api.read_all(_record(), None, runId=str(RUN)):
        _assert_error(response, 401, "UNAUTHENTICATED")


def test_live_decisions_are_not_served(api):
    record = _record(run=None)
    api.save(record)

    for response in api.read_all(record):
        _assert_error(response, 403, "C10.FORBIDDEN")
    for response in api.read_all(record, runId=str(RUN)):
        _assert_error(response, 404, "C10.DECISION_NOT_FOUND")


def test_a_run_that_is_not_shared_is_forbidden(api):
    record = _record(run=PRIVATE_RUN)
    api.save(record)

    for response in api.read_all(record, runId=str(PRIVATE_RUN)):
        _assert_error(response, 403, "C10.FORBIDDEN")
    for response in api.read_all(record, runId=str(RUN)):
        _assert_error(response, 404, "C10.DECISION_NOT_FOUND")


def test_several_decisions_list_the_candidates(api):
    first = _record()
    subject = first.subject.model_dump(mode="json") | {"version": 2}
    second = _record(
        decisionId=str(uuid4()), idempotencyKey=str(uuid4()), subject=subject
    )
    api.save(first, second)
    query = {"subjectType": "Plan", "subjectId": str(first.subject.id)}

    ambiguous = api.get("", runId=str(RUN), **query)
    chosen = api.get("", runId=str(RUN), subjectVersion=2, **query)

    error = _assert_error(ambiguous, 409, "C10.DECISION_AMBIGUOUS")
    assert error["details"]["candidates"] == [
        {"decisionId": str(first.decision_id), "subjectVersion": 1},
        {"decisionId": str(second.decision_id), "subjectVersion": 2},
    ]
    assert chosen.json()["decisionId"] == str(second.decision_id)


def test_evidence_outside_the_record_is_not_found(api):
    record = _record()
    api.save(record)

    response = api.get(f"/{record.decision_id}/evidence/opt-99", runId=str(RUN))

    _assert_error(response, 404, "C10.EVIDENCE_NOT_FOUND")


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/not-a-uuid", {}),
        ("", {"subjectId": str(uuid4())}),
        ("", {"subjectType": "PlanningAttempt", "subjectId": str(uuid4())}),
        ("", {"subjectType": "Plan", "subjectId": str(uuid4()), "subjectVersion": 0}),
    ],
    ids=["decision-id", "no-subject-type", "subject-type", "subject-version"],
)
def test_malformed_requests_are_validation_errors(api, path, params):
    response = api.get(path, runId=str(RUN), **params)

    _assert_error(response, 422, "VALIDATION_FAILED")


def test_storage_failures_are_unavailable_without_internals(api):
    api.store.available = False
    stored = api.read_all(_record(), runId=str(RUN))

    def refuse():
        raise psycopg.OperationalError("connection to 10.0.0.1 refused")

    api.client.app.state.decision_connection = refuse
    for response in [*stored, *api.read_all(_record(), runId=str(RUN))]:
        error = _assert_error(response, 503, "C10.UNAVAILABLE")
        assert (
            error["message"] == "Decision records are unavailable. Try again shortly."
        )


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/decisions",
        "/api/v1/decisions/{decisionId}",
        "/api/v1/decisions/{decisionId}/evidence/{referenceId}",
    ],
)
def test_routes_declare_what_the_contract_does(api, path):
    contract = OPENAPI["paths"][path]["get"]
    served = api.client.get("/openapi.json").json()["paths"][path]["get"]

    assert served["operationId"] == contract["operationId"]
    assert set(served["responses"]) == set(contract["responses"])
    # the only $ref parameter is C02's X-Acting-Provider
    declared = {p.get("name", "X-Acting-Provider") for p in contract["parameters"]}
    assert {p["name"] for p in served["parameters"]} == declared


def test_reads_a_committed_decision_from_postgres(api, test_database_url):
    record = _record()
    service = _service(PostgresDecisionStore())
    with psycopg.connect(test_database_url) as conn:
        conn.execute("truncate c10_decision_records.decisions")
        assert isinstance(service.record_decision(conn, C05, record), Ok)
    state = api.client.app.state
    state.decisions = service
    state.decision_connection = lambda: psycopg.connect(test_database_url)

    _assert_read(api.read_all(record, runId=str(RUN)), record)
