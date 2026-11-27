"""C10 evidence objects in Supabase Storage (#130), against a fake HTTP layer.
Real connectivity is checked by the walking skeleton (backend/README.md)."""

import io
import json
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from app.components.c09_logistics_knowledge.contracts import EvidenceSnapshot
from app.components.c10_decision_records.contracts import DecisionRecord
from app.components.c10_decision_records.evidence_storage import (
    SupabaseEvidenceStorage,
    from_settings,
    object_key,
)
from app.components.c10_decision_records.service import DecisionRecords
from app.components.c10_decision_records.store import (
    InMemoryDecisionStore,
    StoreUnavailableError,
)
from app.shared.c09_c10_common import AuthContext

EXAMPLE = (
    Path(__file__).resolve().parents[2]
    / "contracts/components/c10-decision-records/examples/record-decision-request.json"
)

PROJECT = "https://example.supabase.co"
JWT = "eyJhbGciOi.payload.sig"
DECISION = UUID("d7c6b5a4-9384-4756-a1b2-c3d4e5f60708")


def synthetic_snapshot() -> dict:
    """The contract example's snapshot with a fresh snapshotId (synthetic data)."""
    snapshot = json.loads(EXAMPLE.read_text(encoding="utf-8"))["evidenceSnapshots"][0]
    snapshot["snapshotId"] = str(uuid4())
    EvidenceSnapshot.model_validate(snapshot)  # same shape C10 stores
    return snapshot


class FakeStorage:
    """Records requests; GET returns what POST stored under the same key."""

    def __init__(self, fail_with: int | None = None):
        self.requests = []
        self.objects = {}
        self.fail_with = fail_with

    def body(self) -> bytes:
        if self.fail_with in (400, 409):
            return b'{"statusCode":"409","error":"Duplicate"}'
        return b'{"error":"unavailable"}'

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.fail_with:
            raise urllib.error.HTTPError(
                request.full_url, self.fail_with, "err", {}, io.BytesIO(self.body())
            )
        path = request.full_url.removeprefix(f"{PROJECT}/storage/v1/object/")
        if request.get_method() == "POST":
            self.objects[path] = request.data
            return io.BytesIO(b'{"Key": "ok"}')
        return io.BytesIO(self.objects[path.removeprefix("authenticated/")])


def test_snapshot_round_trips_under_the_c10_prefix_without_overwrite():
    fake = FakeStorage()
    storage = SupabaseEvidenceStorage(PROJECT, JWT, "c10-evidence", fake)
    snapshot = synthetic_snapshot()

    key = storage.put_snapshot(DECISION, snapshot)

    assert key == object_key(DECISION, UUID(snapshot["snapshotId"]))
    assert key.startswith(f"c10/evidence/{DECISION}/")
    upload = fake.requests[0]
    assert upload.full_url == f"{PROJECT}/storage/v1/object/c10-evidence/{key}"
    assert upload.get_header("X-upsert") == "false"
    assert storage.get_snapshot(key) == snapshot


def test_key_is_sent_as_apikey_and_bearer():
    """Storage rejects a request without `authorization` (400 InvalidRequest)."""
    fake = FakeStorage()
    SupabaseEvidenceStorage(PROJECT, JWT, "b", fake).put_snapshot(
        DECISION, synthetic_snapshot()
    )
    assert fake.requests[0].get_header("Apikey") == JWT
    assert fake.requests[0].get_header("Authorization") == f"Bearer {JWT}"


@pytest.mark.parametrize("key", ["sb_secret_x", "sb_publishable_x"])
def test_non_jwt_keys_are_rejected_before_any_request(key):
    """Storage answers 403 "Invalid Compact JWS" for them; say why up front."""
    fake = FakeStorage()
    with pytest.raises(ValueError, match="legacy service_role"):
        SupabaseEvidenceStorage(PROJECT, key, "b", fake)
    assert fake.requests == []


def test_http_error_is_reported_as_unavailable():
    storage = SupabaseEvidenceStorage(PROJECT, JWT, "b", FakeStorage(fail_with=403))
    with pytest.raises(StoreUnavailableError, match="POST 403"):
        storage.put_snapshot(DECISION, synthetic_snapshot())


@pytest.mark.parametrize("status", [409, 400])  # Storage may send 400 + "Duplicate"
def test_existing_object_counts_as_stored(status):
    """A retried decision finds its immutable object already there."""
    storage = SupabaseEvidenceStorage(PROJECT, JWT, "b", FakeStorage(fail_with=status))
    snapshot = synthetic_snapshot()
    assert storage.put_snapshot(DECISION, snapshot) == object_key(
        DECISION, UUID(snapshot["snapshotId"])
    )


# ---- recordDecision uploads the evidence it records ----

C05 = AuthContext(
    principal_id="service:c05-planner",
    principal_type="Service",
    roles=["Service:C05"],
    correlation_id=UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
)


def _record_with(fake) -> tuple[DecisionRecord, DecisionRecords, InMemoryDecisionStore]:
    record = DecisionRecord.model_validate(
        json.loads(EXAMPLE.read_text(encoding="utf-8"))
    )
    store = InMemoryDecisionStore()
    storage = SupabaseEvidenceStorage(PROJECT, JWT, "c10-evidence", fake)
    service = DecisionRecords(store, clock=lambda: datetime.now(UTC), evidence=storage)
    return record, service, store


def test_record_decision_uploads_each_snapshot():
    fake = FakeStorage()
    record, service, store = _record_with(fake)

    assert not service.record_decision(store.begin(), C05, record).value.duplicate

    snap = record.evidence_snapshots[0]
    key = f"c10-evidence/{object_key(record.decision_id, snap.snapshot_id)}"
    assert json.loads(fake.objects[key]) == snap.model_dump(mode="json", by_alias=True)


def test_duplicate_record_does_not_upload_again():
    fake = FakeStorage()
    record, service, store = _record_with(fake)
    tx = store.begin()
    service.record_decision(tx, C05, record)
    tx.commit()

    assert service.record_decision(store.begin(), C05, record).value.duplicate
    assert len(fake.requests) == 1


def test_storage_outage_does_not_fail_the_decision(caplog):
    record, service, store = _record_with(FakeStorage(fail_with=503))
    result = service.record_decision(store.begin(), C05, record)
    assert not result.value.duplicate  # still Ok: Postgres holds the evidence
    assert "evidence upload failed" in caplog.text


def test_storage_is_off_until_configured():
    unset = SimpleNamespace(
        supabase_url=PROJECT, supabase_service_key=None, c10_evidence_bucket="b"
    )
    assert from_settings(unset) is None
    empty = SimpleNamespace(**{**vars(unset), "supabase_service_key": SecretStr("")})
    assert from_settings(empty) is None
    configured = SimpleNamespace(
        supabase_url=PROJECT,
        supabase_service_key=SecretStr(JWT),
        c10_evidence_bucket="b",
    )
    assert isinstance(from_settings(configured), SupabaseEvidenceStorage)


def test_each_smoke_run_uses_a_fresh_snapshot_id():
    assert synthetic_snapshot()["snapshotId"] != synthetic_snapshot()["snapshotId"]
