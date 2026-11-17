"""UC09 ``queryKnowledge``: return applicable policy passages with evidence."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from app.components.c09_logistics_knowledge.contracts import (
    EvidenceSnapshot,
    KnowledgeResult,
    Passage,
    PolicyQuery,
    RetrievalStatus,
)
from app.components.c09_logistics_knowledge.store import (
    KnowledgeStore,
    StoreUnavailableError,
)
from app.shared.c09_c10_common import AuthContext
from app.shared.result import Failure, Ok, Result

QUERY_ROLES = {"Service:C05", "Service:C08", "LogisticsOperator"}
LOW_CONFIDENCE_THRESHOLD = 0.60


def _applies(passage: Passage, query: PolicyQuery) -> bool:
    a = passage.applicability
    window = query.time_window
    return (
        bool(set(a.zone_ids) & set(query.area.zone_ids))
        and (not a.vehicle_classes or query.vehicle_class in a.vehicle_classes)
        and (not a.activities or query.activity in a.activities)
        and passage.valid_from < window.end
        and (passage.valid_to is None or passage.valid_to > window.start)
    )


class LogisticsKnowledge:
    """C09 service API (UC09)."""

    def __init__(
        self,
        store: KnowledgeStore,
        clock: Callable[[], datetime],
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._store = store
        self._clock = clock
        self._new_id = new_id

    def query_knowledge(
        self, ctx: AuthContext, query: PolicyQuery
    ) -> Result[KnowledgeResult]:
        """Filter the active corpus by applicability and rank by score.

        shortcut: ranks by the stored score only; no embedding search or
        conflict detection yet (later commits of #14).
        """
        if not QUERY_ROLES & set(ctx.roles):
            return Failure("Forbidden", "C09.FORBIDDEN")
        try:
            unknown = set(query.area.zone_ids) - self._store.known_zone_ids()
            if unknown:
                return Failure("Invalid", f"C09.UNKNOWN_ZONE: {sorted(unknown)}")
            info = self._store.corpus_info()
            candidates = self._store.active_passages()
        except StoreUnavailableError as exc:
            return Failure("Unavailable", f"C09.INDEX_UNAVAILABLE: {exc}")

        passages = sorted(
            (p for p in candidates if _applies(p, query)),
            key=lambda p: (-p.score, str(p.passage_id)),
        )[: query.max_passages]
        limitations: list[str] = []
        if not passages:
            status = RetrievalStatus.NO_APPLICABLE
            limitations.append("No curated policy applies to this query.")
        elif passages[0].score < LOW_CONFIDENCE_THRESHOLD:
            status = RetrievalStatus.LOW_CONFIDENCE
            limitations.append("Best match is below the confidence threshold.")
        else:
            status = RetrievalStatus.OK

        common = {
            "conflicts": [],
            "limitations": limitations,
            "corpus_version": info.corpus_version,
            "index_version": info.index_version,
            "embedding_model": info.embedding_model,
            "retrieved_at": self._clock(),
        }
        snapshot = EvidenceSnapshot(
            snapshot_id=self._new_id(),
            query=query,
            retrieval_status=status,
            items=passages,
            **common,
        )
        return Ok(
            KnowledgeResult(
                status=status,
                passages=passages,
                evidence_snapshot=snapshot,
                **common,
            )
        )
