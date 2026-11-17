"""Knowledge corpus persistence port for C09, plus an in-memory test double."""

from dataclasses import dataclass, field
from typing import Protocol

from app.components.c09_logistics_knowledge.contracts import Passage


class StoreUnavailableError(Exception):
    """The corpus or vector index cannot be reached; callers report Unavailable."""


@dataclass(frozen=True)
class CorpusInfo:
    """Version metadata of the currently active corpus."""

    corpus_version: int
    index_version: str
    embedding_model: str


class KnowledgeStore(Protocol):
    """Read side of the active corpus.

    Raises:
        StoreUnavailableError: from any method when storage is unreachable.
    """

    def known_zone_ids(self) -> set[str]: ...

    def corpus_info(self) -> CorpusInfo: ...

    def active_passages(self) -> list[Passage]: ...


@dataclass
class InMemoryKnowledgeStore:
    """Test double: holds passages of one corpus version in memory.

    shortcut: no embeddings; passages carry a fixed score. Replace with the
    Postgres/pgvector store once the tables and embedding model are agreed.
    """

    zone_ids: set[str]
    passages: list[Passage] = field(default_factory=list)
    info: CorpusInfo = field(
        default_factory=lambda: CorpusInfo(1, "idx-1", "embedding-model-tbd")
    )
    available: bool = True

    def _check(self) -> None:
        if not self.available:
            raise StoreUnavailableError("knowledge store is unavailable")

    def known_zone_ids(self) -> set[str]:
        self._check()
        return set(self.zone_ids)

    def corpus_info(self) -> CorpusInfo:
        self._check()
        return self.info

    def active_passages(self) -> list[Passage]:
        self._check()
        return list(self.passages)
