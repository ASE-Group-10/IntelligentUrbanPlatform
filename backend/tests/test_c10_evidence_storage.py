"""C10 evidence objects in Supabase Storage (#130), against a fake HTTP layer.
Real connectivity is checked by ``python -m app.demo.c10_storage``."""

import io
import urllib.error
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import SecretStr

from app.components.c10_decision_records.evidence_storage import (
    SupabaseEvidenceStorage,
    from_settings,
    object_key,
)
from app.components.c10_decision_records.store import StoreUnavailableError
from app.demo.c10_storage import synthetic_snapshot

PROJECT = "https://example.supabase.co"
DECISION = UUID("d7c6b5a4-9384-4756-a1b2-c3d4e5f60708")


class FakeStorage:
    """Records requests; GET returns what POST stored under the same key."""

    def __init__(self, fail_with: int | None = None):
        self.requests = []
        self.objects = {}
        self.fail_with = fail_with

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.fail_with:
            raise urllib.error.HTTPError(
                request.full_url, self.fail_with, "err", {}, io.BytesIO(b"Duplicate")
            )
        path = request.full_url.removeprefix(f"{PROJECT}/storage/v1/object/")
        if request.get_method() == "POST":
            self.objects[path] = request.data
            return io.BytesIO(b'{"Key": "ok"}')
        return io.BytesIO(self.objects[path.removeprefix("authenticated/")])


def test_snapshot_round_trips_under_the_c10_prefix_without_overwrite():
    fake = FakeStorage()
    storage = SupabaseEvidenceStorage(PROJECT, "sb_secret_x", "c10-evidence", fake)
    snapshot = synthetic_snapshot()

    key = storage.put_snapshot(DECISION, snapshot)

    assert key == object_key(DECISION, UUID(snapshot["snapshotId"]))
    assert key.startswith(f"c10/evidence/{DECISION}/")
    upload = fake.requests[0]
    assert upload.full_url == f"{PROJECT}/storage/v1/object/c10-evidence/{key}"
    assert upload.get_header("X-upsert") == "false"
    assert storage.get_snapshot(key) == snapshot


@pytest.mark.parametrize(
    ("key", "bearer"),
    [("sb_secret_x", None), ("eyJhbGciOi.jwt", "Bearer eyJhbGciOi.jwt")],
)
def test_secret_keys_go_in_apikey_and_jwts_also_as_bearer(key, bearer):
    fake = FakeStorage()
    SupabaseEvidenceStorage(PROJECT, key, "b", fake).put_snapshot(
        DECISION, synthetic_snapshot()
    )
    assert fake.requests[0].get_header("Apikey") == key
    assert fake.requests[0].get_header("Authorization") == bearer


def test_http_error_is_reported_as_unavailable():
    storage = SupabaseEvidenceStorage(PROJECT, "k", "b", FakeStorage(fail_with=409))
    with pytest.raises(StoreUnavailableError, match="POST 409: Duplicate"):
        storage.put_snapshot(DECISION, synthetic_snapshot())


def test_storage_is_off_until_configured():
    unset = SimpleNamespace(
        supabase_url=PROJECT, supabase_service_key=None, c10_evidence_bucket="b"
    )
    assert from_settings(unset) is None
    empty = SimpleNamespace(**{**vars(unset), "supabase_service_key": SecretStr("")})
    assert from_settings(empty) is None
    configured = SimpleNamespace(
        supabase_url=PROJECT,
        supabase_service_key=SecretStr("k"),
        c10_evidence_bucket="b",
    )
    assert isinstance(from_settings(configured), SupabaseEvidenceStorage)


def test_each_smoke_run_uses_a_fresh_snapshot_id():
    assert synthetic_snapshot()["snapshotId"] != synthetic_snapshot()["snapshotId"]
