"""C09 service API: UC09 publishKnowledge, queryKnowledge, notifyKnowledgeConflict."""

import hashlib
from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from app.components.c09_logistics_knowledge.conflicts import detect_conflicts
from app.components.c09_logistics_knowledge.contracts import (
    ConflictDetails,
    EvidenceSnapshot,
    KnowledgeConflict,
    KnowledgePublication,
    KnowledgeResult,
    PolicyQuery,
    PublishKnowledgeRequest,
    RetrievalStatus,
    RuleCoverage,
    WarningReceipt,
)
from app.components.c09_logistics_knowledge.embedding import (
    Embedder,
    EmbeddingUnavailableError,
    HashingEmbedder,
)
from app.components.c09_logistics_knowledge.sections import (
    InvalidDocumentError,
    split_sections,
)
from app.components.c09_logistics_knowledge.store import (
    IdempotencyMismatchError,
    KnowledgeStore,
    NewChunk,
    NewPublication,
    StoreUnavailableError,
)
from app.shared.c09_c10_common import AuthContext
from app.shared.result import Failure, Ok, Result

QUERY_ROLES = {"Service:C05", "Service:C08", "LogisticsOperator"}
CURATOR_ROLE = "KnowledgeCurator"
LOW_CONFIDENCE_THRESHOLD = 0.60
CONFLICT_LIMITATION = "Conflicting sources; curator has been notified."


class LogisticsKnowledge:
    """C09 service API (UC09)."""

    def __init__(
        self,
        store: KnowledgeStore,
        clock: Callable[[], datetime],
        new_id: Callable[[], UUID] = uuid4,
        embedder: Embedder | None = None,
        low_confidence_threshold: float = LOW_CONFIDENCE_THRESHOLD,
    ) -> None:
        self._store = store
        self._clock = clock
        self._new_id = new_id
        self._embedder = embedder or HashingEmbedder()
        self._threshold = low_confidence_threshold

    # ---- publishKnowledge (contract §7.7) ----
    def publish_knowledge(
        self, ctx: AuthContext, request: PublishKnowledgeRequest
    ) -> Result[KnowledgePublication]:
        """Split a curated Markdown document into sections, embed them and
        activate it as a new document version and corpus version."""
        if CURATOR_ROLE not in ctx.roles:
            return Failure("Forbidden", "C09.NOT_CURATOR")
        request_hash = hashlib.sha256(
            request.model_dump_json(by_alias=True).encode()
        ).hexdigest()
        try:
            receipt = self._store.find_receipt(request.idempotency_key)
        except StoreUnavailableError as exc:
            return Failure("Unavailable", f"C09.INDEXING_FAILED: {exc}")
        if receipt is not None:
            if receipt.request_hash != request_hash:
                return Failure("Conflict", "C09.IDEMPOTENCY_MISMATCH")
            return Ok(receipt.publication.model_copy(update={"duplicate": True}))

        try:
            sections = split_sections(request.document.content)
        except InvalidDocumentError as exc:
            return Failure("Invalid", f"C09.INVALID_DOCUMENT: {exc}")
        by_section = request.metadata.constraints_by_section or {}
        unknown = set(by_section) - {s.section_id for s in sections}
        if unknown:
            return Failure(
                "Invalid", f"C09.INVALID_DOCUMENT: unknown sectionId {sorted(unknown)}"
            )

        title = request.document.title
        try:
            vectors = self._embedder.embed(
                [f"{title}\n{s.heading}\n{s.content}" for s in sections]
            )
            publication = self._store.publish(
                NewPublication(
                    idempotency_key=request.idempotency_key,
                    request_hash=request_hash,
                    document=request.document,
                    metadata=request.metadata,
                    chunks=[
                        NewChunk(
                            passage_id=self._new_id(),
                            section_id=s.section_id,
                            content=s.content,
                            constraints=by_section.get(s.section_id, []),
                            vector=v,
                        )
                        for s, v in zip(sections, vectors, strict=True)
                    ],
                    embedding_model=self._embedder.model_name,
                    published_at=self._clock(),
                )
            )
        except IdempotencyMismatchError:
            # a concurrent request with this key but another body committed first
            return Failure("Conflict", "C09.IDEMPOTENCY_MISMATCH")
        except (EmbeddingUnavailableError, StoreUnavailableError) as exc:
            # nothing was stored, so the previous corpus stays searchable
            return Failure("Unavailable", f"C09.INDEXING_FAILED: {exc}")
        return Ok(publication)

    # ---- queryKnowledge (contract §7.6) ----
    def query_knowledge(
        self, ctx: AuthContext, query: PolicyQuery
    ) -> Result[KnowledgeResult]:
        """Return applicable passages of the active corpus, ranked by similarity
        to ``freeText`` (unranked when there is none), with an EvidenceSnapshot."""
        if not QUERY_ROLES & set(ctx.roles):
            return Failure("Forbidden", "C09.FORBIDDEN")
        try:
            query_vector = (
                self._embedder.embed([query.free_text])[0] if query.free_text else None
            )
        except EmbeddingUnavailableError as exc:
            return Failure("Unavailable", f"C09.EMBEDDING_UNAVAILABLE: {exc}")
        try:
            unknown = set(query.area.zone_ids) - self._store.known_zone_ids()
            if unknown:
                return Failure("Invalid", f"C09.UNKNOWN_ZONE: {sorted(unknown)}")
            info, candidates = self._store.search(query, query_vector)
            if info is None:
                return Failure(
                    "Unavailable", "C09.INDEX_UNAVAILABLE: no corpus published yet"
                )
        except StoreUnavailableError as exc:
            return Failure("Unavailable", f"C09.INDEX_UNAVAILABLE: {exc}")

        ranked = sorted(candidates, key=lambda p: (-p.score, str(p.passage_id)))
        # hard rules are never ranked away: maxPassages limits rule-free passages only
        rules = [p for p in ranked if p.constraints]
        rule_free = [p for p in ranked if not p.constraints][: query.max_passages]
        passages = sorted(
            rules + rule_free, key=lambda p: (-p.score, str(p.passage_id))
        )
        conflicts = detect_conflicts(rules, query, info.corpus_version)
        limitations: list[str] = []
        if not passages:
            status = RetrievalStatus.NO_APPLICABLE
            limitations.append("No curated policy applies to this query.")
        elif conflicts:
            status = RetrievalStatus.CONFLICT
            limitations.append(CONFLICT_LIMITATION)
            for details in conflicts:
                # shortcut: synchronous; the contract allows async. A failed
                # warning must not fail the caller's query.
                self.notify_knowledge_conflict(details)
        elif passages[0].score < self._threshold:
            status = RetrievalStatus.LOW_CONFIDENCE
            limitations.append("Best match is below the confidence threshold.")
        else:
            status = RetrievalStatus.OK

        common = {
            # the store returns every applicable passage, so all hard rules are here
            "rule_coverage": RuleCoverage.COMPLETE,
            "conflicts": [
                KnowledgeConflict(
                    conflict_id=c.conflict_id,
                    passage_ids=c.passage_ids,
                    reason=c.reason,
                )
                for c in conflicts
            ],
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
                status=status, passages=passages, evidence_snapshot=snapshot, **common
            )
        )

    # ---- notifyKnowledgeConflict (contract §7.8, C09-internal) ----
    def notify_knowledge_conflict(
        self, details: ConflictDetails
    ) -> Result[WarningReceipt]:
        """Record a curator warning once per conflict; never blocks planning."""
        try:
            return Ok(self._store.save_warning(details, self._clock()))
        except StoreUnavailableError as exc:
            return Failure("Unavailable", f"C09.WARNING_NOT_SAVED: {exc}")
