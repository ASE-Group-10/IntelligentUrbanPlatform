-- migrate:up
create schema c04_delivery_management;

create table c04_delivery_management.deliveries (
    scope_key text not null check (scope_key ~ '^(live|simulation:[0-9a-f-]{36})$'),
    scope text not null check (scope in ('live', 'simulation')),
    run_id uuid,
    delivery_id uuid not null,
    provider_id uuid not null,
    created_by uuid not null,
    version integer not null check (version >= 1),
    status text not null check (
        status in ('pending', 'planned', 'in_progress', 'delivered', 'cancelled')
    ),
    plan_id uuid,
    planned_at timestamptz,
    delivery jsonb not null,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    primary key (scope_key, delivery_id),
    check (
        (scope = 'live' and run_id is null and scope_key = 'live')
        or (
            scope = 'simulation'
            and run_id is not null
            and scope_key = 'simulation:' || run_id::text
        )
    ),
    check ((delivery ->> 'version')::integer = version),
    check ((delivery ->> 'status') = status)
);

create index deliveries_scope_provider_idx
    on c04_delivery_management.deliveries
    (scope_key, provider_id, status, delivery_id);

create table c04_delivery_management.command_receipts (
    scope_key text not null check (scope_key ~ '^(live|simulation:[0-9a-f-]{36})$'),
    principal_id text not null,
    operation text not null,
    idempotency_key varchar(200) not null,
    request_hash varchar(64) not null,
    response_status integer not null,
    response_body jsonb not null,
    created_at timestamptz not null default now(),
    primary key (scope_key, principal_id, operation, idempotency_key)
);

create table c04_delivery_management.audit_outcomes (
    id uuid primary key,
    scope_key text not null check (scope_key ~ '^(live|simulation:[0-9a-f-]{36})$'),
    provider_id uuid,
    principal_id text not null,
    operation text not null,
    outcome text not null,
    target_ids jsonb not null,
    correlation_id uuid not null,
    details jsonb,
    occurred_at timestamptz not null default now()
);

create index c04_audit_scope_corr_idx
    on c04_delivery_management.audit_outcomes
    (scope_key, correlation_id, occurred_at);

create table c04_delivery_management.outbox (
    seq bigint generated always as identity,
    event_id uuid primary key,
    scope_key text not null check (scope_key ~ '^(live|simulation:[0-9a-f-]{36})$'),
    delivery_id uuid not null,
    delivery_version integer not null check (delivery_version >= 1),
    event_type text not null check (
        event_type in (
            'DeliverySubmitted',
            'DeliveryUpdated',
            'DeliveryProgressChanged'
        )
    ),
    correlation_id uuid not null,
    envelope jsonb not null,
    created_at timestamptz not null default now(),
    published_at timestamptz
);

create index c04_outbox_unpublished_idx
    on c04_delivery_management.outbox (seq)
    where published_at is null;

comment on schema c04_delivery_management is
    'C04 delivery management: run-scoped delivery requests, receipts, audit outcomes, and outbox.';

-- migrate:down
drop schema if exists c04_delivery_management cascade;
