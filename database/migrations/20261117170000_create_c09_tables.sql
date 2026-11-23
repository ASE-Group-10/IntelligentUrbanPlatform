-- migrate:up
create extension if not exists vector;

-- One row per published document version; older versions are kept as Superseded.
create table c09_logistics_knowledge.knowledge_documents (
    document_id      text        not null,
    document_version integer     not null check (document_version >= 1),
    title            text        not null,
    issuer           text        not null,
    format           text        not null check (format = 'markdown'),
    content          text        not null,
    applicability    jsonb       not null,
    valid_from       timestamptz not null,
    valid_to         timestamptz,
    status           text        not null check (status in ('Active', 'Superseded')),
    published_at     timestamptz not null,
    primary key (document_id, document_version)
);
-- At most one active version per document.
create unique index knowledge_documents_one_active
    on c09_logistics_knowledge.knowledge_documents (document_id)
    where status = 'Active';

-- One row per Markdown section of a document version (a "passage").
-- vector(256) matches the stand-in HashingEmbedder; a real embedding model
-- needs a new migration with its own dimension (contract checklist item 15).
create table c09_logistics_knowledge.knowledge_chunks (
    passage_id       uuid    primary key,
    document_id      text    not null,
    document_version integer not null,
    section_id       text    not null,
    content          text    not null check (content <> ''),
    applicability    jsonb   not null,
    constraints      jsonb   not null default '[]',
    embedding        vector(256) not null,
    foreign key (document_id, document_version)
        references c09_logistics_knowledge.knowledge_documents
            (document_id, document_version),
    unique (document_id, document_version, section_id)
);

-- Insert-only: every publish activates a new corpus version.
create table c09_logistics_knowledge.corpus_versions (
    corpus_version  integer     primary key check (corpus_version >= 1),
    index_version   text        not null,
    embedding_model text        not null,
    activated_at    timestamptz not null
);

-- Curator warnings; conflict_id is derived from passages + corpus version,
-- so repeating the same query cannot create a second warning.
create table c09_logistics_knowledge.knowledge_warnings (
    conflict_id    uuid        primary key,
    warning_id     uuid        not null unique,
    passage_ids    uuid[]      not null,
    document_ids   text[]      not null,
    reason         text        not null,
    created_at     timestamptz not null
);

-- publishKnowledge idempotency: same key + same request returns the stored result.
create table c09_logistics_knowledge.publish_receipts (
    idempotency_key text        primary key,
    request_hash    text        not null,
    publication     jsonb       not null,
    created_at      timestamptz not null
);

-- migrate:down
drop table if exists c09_logistics_knowledge.publish_receipts;
drop table if exists c09_logistics_knowledge.knowledge_warnings;
drop table if exists c09_logistics_knowledge.corpus_versions;
drop table if exists c09_logistics_knowledge.knowledge_chunks;
drop table if exists c09_logistics_knowledge.knowledge_documents;
