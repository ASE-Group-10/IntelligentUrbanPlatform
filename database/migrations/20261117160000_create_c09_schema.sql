-- migrate:up
create schema if not exists c09_logistics_knowledge;
comment on schema c09_logistics_knowledge is
    'C09 Logistics Knowledge (UC09): curated corpus, chunks, versions and warnings.';

-- migrate:down
drop schema if exists c09_logistics_knowledge;
