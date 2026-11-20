"""UC09 publishKnowledge and conflict detection acceptance examples (#14)."""

from datetime import UTC, datetime
from uuid import uuid4

from app.components.c09_logistics_knowledge.contracts import (
    PolicyQuery,
    PublishKnowledgeRequest,
    RetrievalStatus,
)
from app.components.c09_logistics_knowledge.embedding import (
    EmbeddingUnavailableError,
    HashingEmbedder,
)
from app.components.c09_logistics_knowledge.service import LogisticsKnowledge
from app.components.c09_logistics_knowledge.store import InMemoryKnowledgeStore
from app.shared.c09_c10_common import AuthContext
from app.shared.result import Failure, Ok
from tests.c09_examples import example as _example

NOW = datetime(2026, 11, 18, 14, 2, tzinfo=UTC)
ZONES = {"dublin-D1", "dublin-D2"}


def _ctx(*roles: str) -> AuthContext:
    return AuthContext(
        principal_id="curator-1",
        principal_type="User",
        roles=list(roles),
        correlation_id=uuid4(),
    )


CURATOR = _ctx("KnowledgeCurator")
PLANNER = _ctx("Service:C05")


def _request(**changes) -> PublishKnowledgeRequest:
    data = _example("publish_knowledge.request.json")
    for path, value in changes.items():
        target = data
        *parents, leaf = path.split("__")
        for key in parents:
            target = target[key]
        target[leaf] = value
    return PublishKnowledgeRequest.model_validate(data)


def _amendment() -> PublishKnowledgeRequest:
    """A second source that bans HGVs only until 10:30 (contradicts 07:00-19:00)."""
    data = _example("publish_knowledge.request.json")
    data["document"]["title"] = "HGV cordon amendment (demo data)"
    data["document"]["content"] = (
        "# 1 Amendment\n\n## 1.1 Morning ban\n\n"
        "Heavy goods vehicles may not enter the cordon between 07:00 and 10:30."
    )
    data["metadata"]["documentId"] = "dcc-hgv-cordon-amendment"
    c = data["metadata"]["constraintsBySection"].pop("4.2")[0]
    c.update(
        constraintId="dcc-hgv-cordon-amendment:v1:1.1:c1",
        timeOfDay={"from": "07:00", "to": "10:30"},
        exemption=None,
    )
    data["metadata"]["constraintsBySection"]["1.1"] = [c]
    data["idempotencyKey"] = "publish:dcc-hgv-cordon-amendment:v1"
    return PublishKnowledgeRequest.model_validate(data)


def _service(store, **kw) -> LogisticsKnowledge:
    return LogisticsKnowledge(store, clock=lambda: NOW, **kw)


def _empty_store() -> InMemoryKnowledgeStore:
    return InMemoryKnowledgeStore(ZONES, info=None)


def _query() -> PolicyQuery:
    return PolicyQuery.model_validate(_example("query_knowledge.request.json"))


# ---- publishKnowledge ----


def test_published_document_becomes_searchable():
    service = _service(_empty_store())
    result = service.publish_knowledge(CURATOR, _request())
    assert isinstance(result, Ok)
    pub = result.value
    assert (pub.document_version, pub.corpus_version, pub.chunk_count) == (1, 1, 1)
    assert pub.duplicate is False

    found = service.query_knowledge(PLANNER, _query())
    assert isinstance(found, Ok)
    [passage] = found.value.passages
    assert passage.section_id == "4.2"
    assert passage.content.startswith("Heavy goods vehicles")
    assert passage.constraints[0].kind == "VehicleBan"
    assert found.value.embedding_model == HashingEmbedder().model_name


def test_same_key_and_body_returns_duplicate():
    service = _service(_empty_store())
    first = service.publish_knowledge(CURATOR, _request())
    again = service.publish_knowledge(CURATOR, _request())
    assert isinstance(again, Ok) and again.value.duplicate is True
    assert again.value.corpus_version == first.value.corpus_version


def test_same_key_different_body_is_conflict():
    service = _service(_empty_store())
    service.publish_knowledge(CURATOR, _request())
    result = service.publish_knowledge(CURATOR, _request(metadata__issuer="Someone"))
    assert result == Failure("Conflict", "C09.IDEMPOTENCY_MISMATCH")


def test_republish_supersedes_previous_version():
    service = _service(_empty_store())
    service.publish_knowledge(CURATOR, _request())
    result = service.publish_knowledge(
        CURATOR,
        _request(
            idempotencyKey="publish:dcc-hgv-cordon:v4",
            document__content="# 4 HGV\n\n## 4.2 Cordon\n\nNo HGVs 06:00-20:00.",
        ),
    )
    assert (result.value.document_version, result.value.corpus_version) == (2, 2)
    [passage] = service.query_knowledge(PLANNER, _query()).value.passages
    assert passage.document_version == 2
    assert "06:00-20:00" in passage.content


def test_non_curator_is_forbidden():
    result = _service(_empty_store()).publish_knowledge(PLANNER, _request())
    assert result == Failure("Forbidden", "C09.NOT_CURATOR")


def test_document_without_text_is_invalid():
    result = _service(_empty_store()).publish_knowledge(
        CURATOR, _request(document__content="# Only a heading")
    )
    assert isinstance(result, Failure) and result.kind == "Invalid"
    assert result.message.startswith("C09.INVALID_DOCUMENT")


def test_constraints_for_unknown_section_are_invalid():
    request = _request()
    request.metadata.constraints_by_section["9.9"] = []
    result = _service(_empty_store()).publish_knowledge(CURATOR, request)
    assert result == Failure(
        "Invalid", "C09.INVALID_DOCUMENT: unknown sectionId ['9.9']"
    )


class _DownEmbedder(HashingEmbedder):
    def embed(self, texts):
        raise EmbeddingUnavailableError("model offline")


def test_indexing_failure_keeps_previous_corpus():
    store = _empty_store()
    _service(store).publish_knowledge(CURATOR, _request())
    result = _service(store, embedder=_DownEmbedder()).publish_knowledge(
        CURATOR, _amendment()
    )
    assert isinstance(result, Failure) and result.kind == "Unavailable"
    assert store.corpus_info().corpus_version == 1


# ---- conflict detection + notifyKnowledgeConflict ----


def test_contradicting_sources_give_conflict_and_one_warning():
    store = _empty_store()
    service = _service(store)
    service.publish_knowledge(CURATOR, _request())
    service.publish_knowledge(CURATOR, _amendment())

    first = service.query_knowledge(PLANNER, _query()).value
    assert first.status is RetrievalStatus.CONFLICT
    assert len(first.passages) == 2
    [conflict] = first.conflicts
    assert set(conflict.passage_ids) == {p.passage_id for p in first.passages}
    assert "07:00-19:00" in conflict.reason and "07:00-10:30" in conflict.reason
    assert first.limitations == ["Conflicting sources; curator has been notified."]

    again = service.query_knowledge(PLANNER, _query()).value
    assert again.conflicts[0].conflict_id == conflict.conflict_id
    assert len(store._warnings) == 1  # repeated query: no second warning


def test_rules_for_other_vehicles_do_not_conflict():
    store = _empty_store()
    service = _service(store)
    service.publish_knowledge(CURATOR, _request())
    data = _amendment().model_dump(mode="json", by_alias=True)
    data["metadata"]["constraintsBySection"]["1.1"][0]["vehicleClasses"] = ["van"]
    service.publish_knowledge(CURATOR, PublishKnowledgeRequest.model_validate(data))
    assert service.query_knowledge(PLANNER, _query()).value.status != "Conflict"


# ---- C05 review of #21: hard rules are not ranked away ----


def test_rule_passages_are_never_cut_by_max_passages():
    store = _empty_store()
    service = _service(store)
    service.publish_knowledge(CURATOR, _request())
    service.publish_knowledge(CURATOR, _amendment())
    data = _example("query_knowledge.request.json")
    data["maxPassages"] = 1
    result = service.query_knowledge(PLANNER, PolicyQuery.model_validate(data)).value
    # both contradicting rules are returned and compared despite maxPassages=1
    assert len(result.passages) == 2
    assert result.status is RetrievalStatus.CONFLICT
    assert result.rule_coverage == "Complete"
    assert result.evidence_snapshot.rule_coverage == "Complete"


def test_rules_on_different_segments_do_not_conflict():
    store = _empty_store()
    service = _service(store)
    service.publish_knowledge(CURATOR, _request())
    data = _amendment().model_dump(mode="json", by_alias=True)
    data["metadata"]["constraintsBySection"]["1.1"][0]["segmentKeys"] = ["seg:111"]
    first = _request().model_dump(mode="json", by_alias=True)
    first["metadata"]["constraintsBySection"]["4.2"][0]["segmentKeys"] = ["seg:222"]
    first["idempotencyKey"] = "publish:dcc-hgv-cordon:segment"
    service.publish_knowledge(CURATOR, PublishKnowledgeRequest.model_validate(first))
    service.publish_knowledge(CURATOR, PublishKnowledgeRequest.model_validate(data))
    assert service.query_knowledge(PLANNER, _query()).value.status != "Conflict"


# ---- TseGong's review of #24: idempotency race in publish ----


class _MissedLookupStore(InMemoryKnowledgeStore):
    """Simulates the race: the early receipt lookup misses a concurrent publish."""

    def find_receipt(self, idempotency_key):
        return None


def test_same_key_different_body_is_conflict_even_when_lookup_races():
    service = _service(_MissedLookupStore(ZONES, info=None))
    service.publish_knowledge(CURATOR, _request())
    result = service.publish_knowledge(CURATOR, _request(metadata__issuer="Someone"))
    assert result == Failure("Conflict", "C09.IDEMPOTENCY_MISMATCH")


def test_same_key_same_body_stays_a_duplicate_when_lookup_races():
    service = _service(_MissedLookupStore(ZONES, info=None))
    first = service.publish_knowledge(CURATOR, _request()).value
    again = service.publish_knowledge(CURATOR, _request()).value
    assert again.duplicate is True
    assert again.corpus_version == first.corpus_version


# ---- TseGong's review of #24 (5917e76): rules in different zones ----


def _scoped(request: PublishKnowledgeRequest, zone: str) -> PublishKnowledgeRequest:
    data = request.model_dump(mode="json", by_alias=True)
    for constraints in data["metadata"]["constraintsBySection"].values():
        for c in constraints:
            c["zoneIds"] = [zone]
    data["idempotencyKey"] += f":{zone}"
    return PublishKnowledgeRequest.model_validate(data)


def _zones_query(*zones: str) -> PolicyQuery:
    data = _example("query_knowledge.request.json")
    data["area"]["zoneIds"] = list(zones)
    data.pop("freeText", None)  # unranked, as in TseGong's scenario
    return PolicyQuery.model_validate(data)


def test_bans_in_different_zones_do_not_conflict_in_a_multi_zone_query():
    store = _empty_store()
    service = _service(store)
    service.publish_knowledge(CURATOR, _scoped(_request(), "dublin-D1"))
    service.publish_knowledge(CURATOR, _scoped(_amendment(), "dublin-D2"))

    for zones in (["dublin-D1"], ["dublin-D2"], ["dublin-D1", "dublin-D2"]):
        result = service.query_knowledge(PLANNER, _zones_query(*zones)).value
        assert result.status is RetrievalStatus.OK, zones
        assert result.conflicts == []
    assert store._warnings == {}  # no curator warning either


def test_bans_sharing_a_queried_zone_still_conflict():
    store = _empty_store()
    service = _service(store)
    service.publish_knowledge(CURATOR, _scoped(_request(), "dublin-D2"))
    service.publish_knowledge(CURATOR, _scoped(_amendment(), "dublin-D2"))
    result = service.query_knowledge(
        PLANNER, _zones_query("dublin-D1", "dublin-D2")
    ).value
    assert result.status is RetrievalStatus.CONFLICT
    assert "in dublin-D2 but" in result.conflicts[0].reason
