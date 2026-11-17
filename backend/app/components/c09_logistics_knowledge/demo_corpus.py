"""Load the synthetic demo corpus in ``fixtures/c09_knowledge`` (thin slice).

Run ``python -m app.components.c09_logistics_knowledge.demo_corpus [--conflict]``
from ``backend/`` with ``DATABASE_URL`` set; it publishes through the C09 service
so the same validation, embedding and versioning run as for a real curator.
"""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.components.c09_logistics_knowledge.contracts import PublishKnowledgeRequest
from app.components.c09_logistics_knowledge.service import LogisticsKnowledge
from app.shared.c09_c10_common import AuthContext
from app.shared.result import Ok

FIXTURES = Path(__file__).resolve().parents[4] / "fixtures" / "c09_knowledge"
DEMO_ZONES = {"dublin-D1", "dublin-D2", "dublin-D8"}
"""shortcut: demo zone list until C06 publishes the shared zone IDs."""

DEMO_CURATOR = AuthContext(
    principal_id="demo-curator",
    principal_type="User",
    roles=["KnowledgeCurator"],
    correlation_id=uuid4(),
)


def demo_requests(include_conflict: bool = False) -> list[PublishKnowledgeRequest]:
    """Publish requests for the demo corpus; the conflict amendment is opt-in."""
    entries = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    return [
        PublishKnowledgeRequest.model_validate(
            {
                "document": {
                    "title": e["title"],
                    "format": "markdown",
                    "content": (FIXTURES / e["file"]).read_text(encoding="utf-8"),
                },
                "metadata": e["metadata"],
                "idempotencyKey": f"demo:{e['metadata']['documentId']}:v1",
            }
        )
        for e in entries
        if include_conflict or e.get("scenario") != "conflict"
    ]


def load(service: LogisticsKnowledge, include_conflict: bool = False) -> list:
    """Publish every demo document; already-published ones come back as duplicates."""
    return [
        service.publish_knowledge(DEMO_CURATOR, r)
        for r in demo_requests(include_conflict)
    ]


def main() -> None:
    from app.components.c09_logistics_knowledge.postgres_store import (
        PostgresKnowledgeStore,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conflict", action="store_true", help="add the amendment")
    args = parser.parse_args()
    dsn = os.environ.get(
        "DATABASE_URL", "postgres://ulop:ulop@localhost:5432/ulop?sslmode=disable"
    )
    service = LogisticsKnowledge(
        PostgresKnowledgeStore(dsn, DEMO_ZONES), clock=lambda: datetime.now(UTC)
    )
    for result in load(service, args.conflict):
        if isinstance(result, Ok):
            p = result.value
            note = " (already published)" if p.duplicate else ""
            print(
                f"{p.document_id} v{p.document_version}: {p.chunk_count} passages{note}"
            )
        else:
            print(f"FAILED {result.kind}: {result.message}")


if __name__ == "__main__":
    main()
