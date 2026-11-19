"""Knowledge corpus persistence port for C09, plus an in-memory test double."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from app.components.c09_logistics_knowledge.contracts import (
    Applicability,
    ConflictDetails,
    Constraint,
    DocumentMetadata,
    KnowledgeDocument,
    KnowledgePublication,
    Passage,
    PolicyQuery,
    WarningReceipt,
)
from app.components.c09_logistics_knowledge.embedding import cosine_score

UNRANKED_SCORE = 1.0
"""Score of an applicable passage when the query has no text to rank by."""


class StoreUnavailableError(Exception):
    """The corpus or vector index cannot be reached; callers report Unavailable."""


class IdempotencyMismatchError(Exception):
    """The idempotency key was already used with a different request."""


@dataclass(frozen=True)
class CorpusInfo:
    """Version metadata of the currently active corpus."""

    corpus_version: int
    index_version: str
    embedding_model: str


@dataclass(frozen=True)
class NewChunk:
    """A section ready to be stored, with its embedding."""

    passage_id: UUID
    section_id: str
    content: str
    constraints: list[Constraint]
    vector: list[float]


@dataclass(frozen=True)
class NewPublication:
    """Everything one publishKnowledge call stores atomically."""

    idempotency_key: str
    request_hash: str
    document: KnowledgeDocument
    metadata: DocumentMetadata
    chunks: list[NewChunk]
    embedding_model: str
    published_at: datetime


@dataclass(frozen=True)
class Receipt:
    request_hash: str
    publication: KnowledgePublication


def applies(applicability: Applicability, valid_from, valid_to, q: PolicyQuery) -> bool:
    """Applicability filter shared by every store (empty list means "all")."""
    window = q.time_window
    return (
        bool(set(applicability.zone_ids) & set(q.area.zone_ids))
        and (
            not applicability.vehicle_classes
            or q.vehicle_class in applicability.vehicle_classes
        )
        and (not applicability.activities or q.activity in applicability.activities)
        and valid_from < window.end
        and (valid_to is None or valid_to > window.start)
    )


class KnowledgeStore(Protocol):
    """C09 storage. Every method may raise ``StoreUnavailableError``."""

    def known_zone_ids(self) -> set[str]: ...

    def corpus_info(self) -> CorpusInfo | None:
        """Active corpus version, or ``None`` before the first publish."""
        ...

    def search(
        self, query: PolicyQuery, query_vector: list[float] | None
    ) -> tuple[CorpusInfo | None, list[Passage]]:
        """The active corpus version and its applicable passages, read from one
        consistent snapshot so a concurrent publish cannot mix versions. Passages
        are scored against ``query_vector`` (``UNRANKED_SCORE`` when ``None``),
        unsorted. ``None`` info means nothing is published yet."""
        ...

    def find_receipt(self, idempotency_key: str) -> Receipt | None: ...

    def publish(self, new: NewPublication) -> KnowledgePublication:
        """Atomically store a new document version, its chunks, a new corpus
        version and the idempotency receipt; supersede the previous version.
        A key already stored returns that publication as ``duplicate`` if the
        request hash matches, else raises ``IdempotencyMismatchError``."""
        ...

    def save_warning(self, details: ConflictDetails, at: datetime) -> WarningReceipt:
        """Store a curator warning once per ``conflict_id``."""
        ...


@dataclass
class _Stored:
    passage: Passage
    vector: list[float] | None


@dataclass
class InMemoryKnowledgeStore:
    """Test double. ``passages`` seeds a corpus with fixed scores (no vectors)."""

    zone_ids: set[str]
    passages: list[Passage] = field(default_factory=list)
    info: CorpusInfo | None = field(
        default_factory=lambda: CorpusInfo(1, "idx-1", "embedding-model-tbd")
    )
    available: bool = True
    _chunks: list[_Stored] = field(default_factory=list)
    _versions: dict[str, int] = field(default_factory=dict)
    _receipts: dict[str, Receipt] = field(default_factory=dict)
    _warnings: dict[UUID, UUID] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._chunks = [_Stored(p, None) for p in self.passages]

    def _check(self) -> None:
        if not self.available:
            raise StoreUnavailableError("knowledge store is unavailable")

    def known_zone_ids(self) -> set[str]:
        self._check()
        return set(self.zone_ids)

    def corpus_info(self) -> CorpusInfo | None:
        self._check()
        return self.info

    def search(
        self, query: PolicyQuery, query_vector: list[float] | None
    ) -> tuple[CorpusInfo | None, list[Passage]]:
        self._check()
        found = []
        for stored in self._chunks:
            p = stored.passage
            if not applies(p.applicability, p.valid_from, p.valid_to, query):
                continue
            if query_vector is not None and stored.vector is not None:
                p = p.model_copy(
                    update={"score": cosine_score(query_vector, stored.vector)}
                )
            found.append(p)
        return self.info, found

    def find_receipt(self, idempotency_key: str) -> Receipt | None:
        self._check()
        return self._receipts.get(idempotency_key)

    def publish(self, new: NewPublication) -> KnowledgePublication:
        self._check()
        if (receipt := self._receipts.get(new.idempotency_key)) is not None:
            if receipt.request_hash != new.request_hash:
                raise IdempotencyMismatchError(new.idempotency_key)
            return receipt.publication.model_copy(update={"duplicate": True})
        meta = new.metadata
        version = self._versions.get(meta.document_id, 0) + 1
        corpus = (self.info.corpus_version if self.info else 0) + 1
        self._chunks = [
            s for s in self._chunks if s.passage.document_id != meta.document_id
        ]
        for chunk in new.chunks:
            passage = Passage(
                passage_id=chunk.passage_id,
                content=chunk.content,
                document_id=meta.document_id,
                document_title=new.document.title,
                document_version=version,
                section_id=chunk.section_id,
                valid_from=meta.valid_from,
                valid_to=meta.valid_to,
                applicability=meta.applicability,
                score=UNRANKED_SCORE,
                constraints=chunk.constraints,
            )
            self._chunks.append(_Stored(passage, chunk.vector))
        self._versions[meta.document_id] = version
        self.info = CorpusInfo(corpus, f"idx-{corpus}", new.embedding_model)
        publication = KnowledgePublication(
            document_id=meta.document_id,
            document_version=version,
            corpus_version=corpus,
            index_version=self.info.index_version,
            chunk_count=len(new.chunks),
            activated_at=new.published_at,
            duplicate=False,
        )
        self._receipts[new.idempotency_key] = Receipt(new.request_hash, publication)
        return publication

    def save_warning(self, details: ConflictDetails, at: datetime) -> WarningReceipt:
        self._check()
        if details.conflict_id in self._warnings:
            return WarningReceipt(
                warning_id=self._warnings[details.conflict_id], duplicate=True
            )
        self._warnings[details.conflict_id] = warning_id = uuid4()
        return WarningReceipt(warning_id=warning_id, duplicate=False)
