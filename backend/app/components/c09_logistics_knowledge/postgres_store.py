"""PostgreSQL + pgvector implementation of the C09 ``KnowledgeStore``."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.components.c09_logistics_knowledge.contracts import (
    ConflictDetails,
    KnowledgePublication,
    Passage,
    PolicyQuery,
    WarningReceipt,
)
from app.components.c09_logistics_knowledge.store import (
    UNRANKED_SCORE,
    CorpusInfo,
    NewPublication,
    Receipt,
    StoreUnavailableError,
)

S = "c09_logistics_knowledge"

_SEARCH = f"""
select c.passage_id, c.content, c.document_id, d.title as document_title,
       c.document_version, c.section_id, d.valid_from, d.valid_to,
       c.applicability, c.constraints,
       case when %(qv)s::vector is null then %(unranked)s
            else greatest(0, 1 - (c.embedding <=> %(qv)s::vector)) end as score
from {S}.knowledge_chunks c
join {S}.knowledge_documents d using (document_id, document_version)
where d.status = 'Active'
  and c.applicability -> 'zoneIds' ?| %(zones)s
  and (jsonb_array_length(c.applicability -> 'vehicleClasses') = 0
       or c.applicability -> 'vehicleClasses' ? %(vehicle)s)
  and (jsonb_array_length(c.applicability -> 'activities') = 0
       or c.applicability -> 'activities' ? %(activity)s)
  and d.valid_from < %(end)s
  and (d.valid_to is null or d.valid_to > %(start)s)
"""


def _vector(values: list[float] | None) -> str | None:
    return None if values is None else "[" + ",".join(map(repr, values)) + "]"


class PostgresKnowledgeStore:
    """Opens a short connection per call; each publish is one transaction.

    ``zone_ids``: shortcut: the known zone list is configuration until C06
    publishes the shared zone list (contract checklist item 14).
    """

    def __init__(self, dsn: str, zone_ids: set[str]) -> None:
        self._dsn = dsn
        self._zone_ids = set(zone_ids)

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection]:
        """Commit on success, roll back on error; outages become Unavailable."""
        try:
            with psycopg.connect(
                self._dsn, row_factory=dict_row, connect_timeout=5
            ) as conn:
                yield conn
        except psycopg.OperationalError as exc:
            raise StoreUnavailableError(str(exc)) from exc

    def known_zone_ids(self) -> set[str]:
        return set(self._zone_ids)

    def corpus_info(self) -> CorpusInfo | None:
        with self._connect() as conn:
            row = conn.execute(
                f"select corpus_version, index_version, embedding_model "
                f"from {S}.corpus_versions order by corpus_version desc limit 1"
            ).fetchone()
        return CorpusInfo(**row) if row else None

    def search(
        self, query: PolicyQuery, query_vector: list[float] | None
    ) -> list[Passage]:
        params = {
            "qv": _vector(query_vector),
            "unranked": UNRANKED_SCORE,
            "zones": query.area.zone_ids,
            "vehicle": query.vehicle_class.value,
            "activity": query.activity.value,
            "start": query.time_window.start,
            "end": query.time_window.end,
        }
        with self._connect() as conn:
            rows = conn.execute(_SEARCH, params).fetchall()
        return [
            Passage.model_validate({**r, "score": round(float(r["score"]), 4)})
            for r in rows
        ]

    def find_receipt(self, idempotency_key: str) -> Receipt | None:
        with self._connect() as conn:
            row = conn.execute(
                f"select request_hash, publication from {S}.publish_receipts "
                f"where idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return Receipt(
            row["request_hash"], KnowledgePublication.model_validate(row["publication"])
        )

    def publish(self, new: NewPublication) -> KnowledgePublication:
        meta = new.metadata
        applicability = Jsonb(meta.applicability.model_dump(mode="json", by_alias=True))
        with self._connect() as conn, conn.transaction():
            # serialise publishes so version numbers cannot collide
            conn.execute("select pg_advisory_xact_lock(hashtext('c09.publish'))")
            receipt = conn.execute(
                f"select publication from {S}.publish_receipts "
                f"where idempotency_key = %s",
                (new.idempotency_key,),
            ).fetchone()
            if receipt:  # a concurrent retry won the race
                return KnowledgePublication.model_validate(
                    {**receipt["publication"], "duplicate": True}
                )
            version = conn.execute(
                f"select coalesce(max(document_version), 0) + 1 as v "
                f"from {S}.knowledge_documents where document_id = %s",
                (meta.document_id,),
            ).fetchone()["v"]
            corpus = conn.execute(
                f"select coalesce(max(corpus_version), 0) + 1 as v "
                f"from {S}.corpus_versions"
            ).fetchone()["v"]
            conn.execute(
                f"update {S}.knowledge_documents set status = 'Superseded' "
                f"where document_id = %s and status = 'Active'",
                (meta.document_id,),
            )
            conn.execute(
                f"insert into {S}.knowledge_documents (document_id, document_version,"
                f" title, issuer, format, content, applicability, valid_from,"
                f" valid_to, status, published_at)"
                f" values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'Active', %s)",
                (
                    meta.document_id,
                    version,
                    new.document.title,
                    meta.issuer,
                    new.document.format,
                    new.document.content,
                    applicability,
                    meta.valid_from,
                    meta.valid_to,
                    new.published_at,
                ),
            )
            with conn.cursor() as cur:
                cur.executemany(
                    f"insert into {S}.knowledge_chunks (passage_id, document_id,"
                    f" document_version, section_id, content, applicability,"
                    f" constraints, embedding)"
                    f" values (%s, %s, %s, %s, %s, %s, %s, %s::vector)",
                    [
                        (
                            c.passage_id,
                            meta.document_id,
                            version,
                            c.section_id,
                            c.content,
                            applicability,
                            Jsonb(
                                [
                                    x.model_dump(mode="json", by_alias=True)
                                    for x in c.constraints
                                ]
                            ),
                            _vector(c.vector),
                        )
                        for c in new.chunks
                    ],
                )
            publication = KnowledgePublication(
                document_id=meta.document_id,
                document_version=version,
                corpus_version=corpus,
                index_version=f"idx-{corpus}",
                chunk_count=len(new.chunks),
                activated_at=new.published_at,
                duplicate=False,
            )
            conn.execute(
                f"insert into {S}.corpus_versions values (%s, %s, %s, %s)",
                (
                    corpus,
                    publication.index_version,
                    new.embedding_model,
                    new.published_at,
                ),
            )
            conn.execute(
                f"insert into {S}.publish_receipts values (%s, %s, %s, %s)",
                (
                    new.idempotency_key,
                    new.request_hash,
                    Jsonb(publication.model_dump(mode="json", by_alias=True)),
                    new.published_at,
                ),
            )
        return publication

    def save_warning(self, details: ConflictDetails, at: datetime) -> WarningReceipt:
        with self._connect() as conn:
            row = conn.execute(
                f"insert into {S}.knowledge_warnings (conflict_id, warning_id,"
                f" passage_ids, document_ids, reason, created_at)"
                f" values (%s, %s, %s, %s, %s, %s)"
                f" on conflict (conflict_id) do nothing returning warning_id",
                (
                    details.conflict_id,
                    uuid4(),
                    details.passage_ids,
                    details.document_ids,
                    details.reason,
                    at,
                ),
            ).fetchone()
            if row:
                return WarningReceipt(warning_id=row["warning_id"], duplicate=False)
            existing: UUID = conn.execute(
                f"select warning_id from {S}.knowledge_warnings where conflict_id = %s",
                (details.conflict_id,),
            ).fetchone()["warning_id"]
        return WarningReceipt(warning_id=existing, duplicate=True)
