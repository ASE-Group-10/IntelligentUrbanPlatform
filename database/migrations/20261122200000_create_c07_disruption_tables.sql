-- migrate:up

-- Disruptions C07 recorded, one row per disruption and City State scope
-- (`live` or `simulation:<runId>`, same keys as C06). The full record is kept
-- as JSON in the contract's shape (contracts/components/c07-disruption-detection).
create table c07_disruption_detection.disruptions (
    scope_key text not null check (scope_key ~ '^(live|simulation:[0-9a-f-]{36})$'),
    disruption_id uuid not null,
    version integer not null check (version >= 1),
    disruption jsonb not null,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    primary key (scope_key, disruption_id)
);
create index disruptions_by_trigger
    on c07_disruption_detection.disruptions using gin ((disruption -> 'triggerEventIds'));

-- Events already evaluated, so a redelivery is recognised as a duplicate.
create table c07_disruption_detection.processed_events (
    scope_key text not null check (scope_key ~ '^(live|simulation:[0-9a-f-]{36})$'),
    event_id uuid not null,
    processed_at timestamptz not null default now(),
    primary key (scope_key, event_id)
);

-- migrate:down
drop table if exists c07_disruption_detection.processed_events;
drop table if exists c07_disruption_detection.disruptions;
