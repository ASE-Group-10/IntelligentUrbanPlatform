-- migrate:up

-- One row per City State: live, or one simulation run. Its version is the
-- counter every change increments; locking this row serialises changes.
create table c06_city_monitoring.city_state (
    scope_key text primary key,
    scope text not null check (scope in ('live', 'simulation')),
    run_id uuid,
    version integer not null default 0 check (version >= 0),
    updated_at timestamptz not null default now(),
    check ((scope = 'live') = (run_id is null)),
    check (
        scope_key = case
            when scope = 'live' then 'live'
            else 'simulation:' || run_id::text
        end
    )
);

-- Every segment condition, kept per City State version so past snapshots can
-- be rebuilt. The current condition is the highest version per segment.
create table c06_city_monitoring.segment_conditions (
    scope_key text not null references c06_city_monitoring.city_state (scope_key),
    segment_key text not null check (segment_key ~ '^seg:[1-9][0-9]*$'),
    version integer not null check (version >= 1),
    condition jsonb not null,
    primary key (scope_key, segment_key, version)
);

-- Raw observations and their ingestion receipts (history, idempotency).
-- seq keeps the order in which they were recorded.
create table c06_city_monitoring.observations (
    seq bigint generated always as identity unique,
    scope_key text not null references c06_city_monitoring.city_state (scope_key),
    observation_id uuid not null,
    observation jsonb not null,
    receipt jsonb not null,
    recorded_at timestamptz not null default now(),
    primary key (scope_key, observation_id)
);

-- Transactional outbox: events written with the state change, published later
-- in seq order.
create table c06_city_monitoring.outbox (
    seq bigint generated always as identity unique,
    event_id uuid primary key,
    scope_key text not null references c06_city_monitoring.city_state (scope_key),
    event_type text not null,
    envelope jsonb not null,
    created_at timestamptz not null default now(),
    published_at timestamptz
);

create index outbox_unpublished
    on c06_city_monitoring.outbox (seq)
    where published_at is null;

-- migrate:down
drop table c06_city_monitoring.outbox;
drop table c06_city_monitoring.observations;
drop table c06_city_monitoring.segment_conditions;
drop table c06_city_monitoring.city_state;
