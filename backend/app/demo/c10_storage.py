"""Smoke test: write synthetic C10 evidence to Supabase Storage, read it back (#130).

From backend/ with SUPABASE_URL and SUPABASE_SERVICE_KEY set:

    python -m app.demo.c10_storage

Exit code 0 and ``OK`` when the object reads back byte-for-byte; 1 and ``FAIL``
otherwise. Each run writes a new object, so runs never collide.
"""

import json
import sys
from pathlib import Path
from uuid import uuid4

from app.components.c09_logistics_knowledge.contracts import EvidenceSnapshot
from app.components.c10_decision_records.evidence_storage import from_settings
from app.components.c10_decision_records.store import StoreUnavailableError
from app.config import get_settings

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "contracts/components/c10-decision-records/examples/record-decision-request.json"
)


def synthetic_snapshot() -> dict:
    """The contract example's snapshot with a fresh snapshotId (synthetic data)."""
    snapshot = json.loads(EXAMPLE.read_text(encoding="utf-8"))["evidenceSnapshots"][0]
    snapshot["snapshotId"] = str(uuid4())
    EvidenceSnapshot.model_validate(snapshot)  # same shape C10 stores
    return snapshot


def main() -> int:
    settings = get_settings()
    storage = from_settings(settings)
    if storage is None:
        print("FAIL: set SUPABASE_URL and SUPABASE_SERVICE_KEY (see backend/README.md)")
        return 1
    snapshot = synthetic_snapshot()
    try:
        key = storage.put_snapshot(uuid4(), snapshot)
        stored = storage.get_snapshot(key)
    except StoreUnavailableError as exc:
        print(f"FAIL: {exc}")
        return 1
    if stored != snapshot:
        print(f"FAIL: {settings.c10_evidence_bucket}/{key} read back different content")
        return 1
    print(f"OK: wrote and read back {settings.c10_evidence_bucket}/{key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
