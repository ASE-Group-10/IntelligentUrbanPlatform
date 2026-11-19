"""C09 against a real PostgreSQL + pgvector database (integration).

Runs only when ``C09_TEST_DATABASE_URL`` points at a migrated database, e.g.
``postgres://ulop:ulop@localhost:5432/ulop?sslmode=disable`` after
``docker compose up -d db`` and ``docker compose run --rm dbmate up``.
It deletes all C09 rows first, so never point it at a shared database.
"""

import os

import psycopg
import pytest

from app.components.c09_logistics_knowledge.contracts import RetrievalStatus
from app.components.c09_logistics_knowledge.postgres_store import (
    PostgresKnowledgeStore,
    S,
)
from app.components.c09_logistics_knowledge.service import LogisticsKnowledge
from app.components.c09_logistics_knowledge.store import StoreUnavailableError
from app.shared.result import Failure, Ok
from tests.test_c09_publish_and_conflicts import (
    CURATOR,
    NOW,
    PLANNER,
    ZONES,
    _amendment,
    _query,
    _request,
)

DSN = os.environ.get("C09_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="C09_TEST_DATABASE_URL not set")


@pytest.fixture
def service() -> LogisticsKnowledge:
    with psycopg.connect(DSN) as conn:
        conn.execute(
            f"truncate {S}.publish_receipts, {S}.knowledge_warnings, "
            f"{S}.corpus_versions, {S}.knowledge_chunks, {S}.knowledge_documents"
        )
    return LogisticsKnowledge(PostgresKnowledgeStore(DSN, ZONES), clock=lambda: NOW)


def test_publish_then_query_round_trip(service):
    pub = service.publish_knowledge(CURATOR, _request())
    assert isinstance(pub, Ok)
    assert (pub.value.document_version, pub.value.corpus_version) == (1, 1)

    result = service.query_knowledge(PLANNER, _query())
    assert isinstance(result, Ok)
    [passage] = result.value.passages
    assert passage.section_id == "4.2"
    assert passage.constraints[0].kind == "VehicleBan"
    assert 0 < passage.score <= 1  # scored by pgvector cosine distance
    assert result.value.corpus_version == 1


def test_duplicate_publish_and_supersede(service):
    service.publish_knowledge(CURATOR, _request())
    again = service.publish_knowledge(CURATOR, _request())
    assert again.value.duplicate is True
    second = service.publish_knowledge(
        CURATOR,
        _request(
            idempotencyKey="publish:dcc-hgv-cordon:v4",
            document__content="# 4 HGV\n\n## 4.2 Cordon\n\nNo HGVs 06:00-20:00.",
        ),
    )
    assert (second.value.document_version, second.value.corpus_version) == (2, 2)
    [passage] = service.query_knowledge(PLANNER, _query()).value.passages
    assert passage.document_version == 2


def test_conflict_is_warned_once(service):
    service.publish_knowledge(CURATOR, _request())
    service.publish_knowledge(CURATOR, _amendment())
    first = service.query_knowledge(PLANNER, _query()).value
    service.query_knowledge(PLANNER, _query())
    assert first.status is RetrievalStatus.CONFLICT
    with psycopg.connect(DSN) as conn:
        [(count,)] = conn.execute(f"select count(*) from {S}.knowledge_warnings")
    assert count == 1


def test_unreachable_database_is_unavailable():
    store = PostgresKnowledgeStore("postgres://x:y@127.0.0.1:1/none", ZONES)
    with pytest.raises(StoreUnavailableError):
        store.corpus_info()
    result = LogisticsKnowledge(store, clock=lambda: NOW).query_knowledge(
        PLANNER, _query()
    )
    assert isinstance(result, Failure) and result.kind == "Unavailable"


# ---- Ye's review of #24 (#14): publication during a query ----


class _PublishDuringQuery(PostgresKnowledgeStore):
    """Commits a new document version from another connection between the
    corpus-version read and the passage read of ``search``."""

    def __init__(self, dsn, zone_ids, publish):
        super().__init__(dsn, zone_ids)
        self._publish = publish

    def _between_reads(self, conn):
        self._publish()


def test_publication_during_a_query_cannot_mix_corpus_versions(service):
    service.publish_knowledge(CURATOR, _request())  # corpus 1, document v1
    republish = _request(
        idempotencyKey="publish:dcc-hgv-cordon:v4",
        document__content="# 4 HGV\n\n## 4.2 Cordon\n\nNo HGVs 06:00-20:00.",
    )
    racing = LogisticsKnowledge(
        _PublishDuringQuery(
            DSN, ZONES, lambda: service.publish_knowledge(CURATOR, republish)
        ),
        clock=lambda: NOW,
    )
    result = racing.query_knowledge(PLANNER, _query()).value
    [passage] = result.passages
    # the query sees one snapshot: corpus 1 with document v1, not a mix
    assert (result.corpus_version, passage.document_version) == (1, 1)
    assert result.evidence_snapshot.corpus_version == 1

    after = service.query_knowledge(PLANNER, _query()).value
    assert (after.corpus_version, after.passages[0].document_version) == (2, 2)


# ---- TseGong's review of #24: idempotency race in publish ----


def _new_publication(request_hash):
    from app.components.c09_logistics_knowledge.store import NewPublication

    req = _request()
    return NewPublication(
        idempotency_key=req.idempotency_key,
        request_hash=request_hash,
        document=req.document,
        metadata=req.metadata,
        chunks=[],
        embedding_model="hashing-bow-256 (stand-in)",
        published_at=NOW,
    )


def test_locked_recheck_compares_the_request_hash(service):
    from app.components.c09_logistics_knowledge.store import IdempotencyMismatchError

    store = PostgresKnowledgeStore(DSN, ZONES)
    first = service.publish_knowledge(CURATOR, _request()).value
    stored_hash = store.find_receipt(_request().idempotency_key).request_hash
    # call the store directly, as a concurrent request that missed the lookup would
    with pytest.raises(IdempotencyMismatchError):
        store.publish(_new_publication("another-body"))
    again = store.publish(_new_publication(stored_hash))
    assert again.duplicate is True and again.corpus_version == first.corpus_version
