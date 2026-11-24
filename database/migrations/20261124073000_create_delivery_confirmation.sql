-- migrate:up
-- Minimal C04 persistence for a delivery already saved in a simulation run.
create schema c04_delivery_management;
create table c04_delivery_management.deliveries (
    run_id uuid not null,
    delivery jsonb not null,
    delivery_id uuid generated always as ((delivery ->> 'deliveryId')::uuid) stored,
    plan_id uuid,
    -- Re-importing the same saved demo input must never overwrite newer C04 state.
    initial_snapshot_hash bytea,
    primary key (run_id, delivery_id),
    check ((delivery ->> 'version')::integer >= 1)
);

create table c04_delivery_management.planning_receipts (
    run_id uuid not null,
    principal_id text not null,
    idempotency_key text not null,
    request_body jsonb not null,
    receipt jsonb not null,
    primary key (run_id, principal_id, idempotency_key)
);

create table c05_planning.confirmation_receipts (
    scope_key text not null,
    principal_id uuid not null,
    provider_id uuid not null,
    idempotency_key text not null,
    plan_id uuid not null,
    expected_version integer not null check (expected_version > 0),
    version integer not null check (version = expected_version + 1),
    primary key (scope_key, principal_id, provider_id, idempotency_key),
    foreign key (scope_key, plan_id, version)
        references c05_planning.plan_versions (scope_key, plan_id, version)
);

-- migrate:down
drop table c05_planning.confirmation_receipts;
drop table c04_delivery_management.planning_receipts;
drop table c04_delivery_management.deliveries;
drop schema c04_delivery_management;
