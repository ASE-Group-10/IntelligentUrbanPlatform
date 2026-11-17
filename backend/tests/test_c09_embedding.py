"""Embedding stand-in and similarity ranking for C09 (#14)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.components.c09_logistics_knowledge.contracts import (
    PolicyQuery,
    PublishKnowledgeRequest,
)
from app.components.c09_logistics_knowledge.embedding import (
    EmbeddingUnavailableError,
    HashingEmbedder,
    cosine_score,
)
from app.components.c09_logistics_knowledge.service import LogisticsKnowledge
from app.components.c09_logistics_knowledge.store import InMemoryKnowledgeStore
from app.shared.c09_c10_common import AuthContext
from app.shared.result import Failure

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "c09_c10" / "examples"
NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


def _ctx(role: str) -> AuthContext:
    return AuthContext(
        principal_id="x", principal_type="User", roles=[role], correlation_id=uuid4()
    )


def _example(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def test_vectors_are_deterministic_unit_length():
    embedder = HashingEmbedder()
    [a] = embedder.embed(["HGV cordon ban"])
    [b] = HashingEmbedder().embed(["HGV cordon ban"])
    assert a == b and len(a) == 256
    assert cosine_score(a, a) == 1.0


def test_similar_text_scores_higher():
    [query, close, far] = HashingEmbedder().embed(
        ["loading bay hours", "loading bay hours on Grafton Street", "HGV permit fees"]
    )
    assert cosine_score(query, close) > cosine_score(query, far)


def test_free_text_ranks_the_matching_section_first():
    data = _example("publish_knowledge.request.json")
    data["document"]["content"] = (
        "# Rules\n\n## Loading\n\nLoading bays may be used 06:00-11:00.\n\n"
        "## Permits\n\nHGV permits are issued by the city council."
    )
    data["metadata"]["constraintsBySection"] = {}
    store = InMemoryKnowledgeStore({"dublin-D2"}, info=None)
    service = LogisticsKnowledge(store, clock=lambda: NOW)
    service.publish_knowledge(
        _ctx("KnowledgeCurator"), PublishKnowledgeRequest.model_validate(data)
    )

    query = _example("query_knowledge.request.json")
    query["freeText"] = "when can I use a loading bay"
    result = service.query_knowledge(
        _ctx("Service:C05"), PolicyQuery.model_validate(query)
    )
    passages = result.value.passages
    assert passages[0].section_id == "loading"
    assert passages[0].score > passages[1].score


def test_query_without_free_text_is_unranked():
    data = _example("publish_knowledge.request.json")
    store = InMemoryKnowledgeStore({"dublin-D2"}, info=None)
    service = LogisticsKnowledge(store, clock=lambda: NOW)
    service.publish_knowledge(
        _ctx("KnowledgeCurator"), PublishKnowledgeRequest.model_validate(data)
    )
    query = _example("query_knowledge.request.json")
    del query["freeText"]
    result = service.query_knowledge(
        _ctx("Service:C05"), PolicyQuery.model_validate(query)
    )
    assert [p.score for p in result.value.passages] == [1.0]


class _DownEmbedder(HashingEmbedder):
    def embed(self, texts):
        raise EmbeddingUnavailableError("model offline")


def test_embedding_outage_on_query_is_unavailable():
    store = InMemoryKnowledgeStore({"dublin-D2"})
    service = LogisticsKnowledge(store, clock=lambda: NOW, embedder=_DownEmbedder())
    result = service.query_knowledge(
        _ctx("Service:C05"),
        PolicyQuery.model_validate(_example("query_knowledge.request.json")),
    )
    assert isinstance(result, Failure)
    assert result.message.startswith("C09.EMBEDDING_UNAVAILABLE")
