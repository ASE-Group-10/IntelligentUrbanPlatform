-- migrate:up
create schema if not exists c03_resource_management;

create table c03_resource_management.resources (
    id uuid primary key default gen_random_uuid(),
    provider_id uuid not null,
    external_identifier varchar(100) not null
        check (btrim(external_identifier) <> ''),
    name varchar(200) not null check (btrim(name) <> ''),
    resource_type varchar(20) not null
        check (resource_type in ('vehicle', 'depot', 'warehouse', 'locker')),
    address text not null check (btrim(address) <> ''),
    latitude numeric(9, 6) not null check (latitude between -90 and 90),
    longitude numeric(9, 6) not null check (longitude between -180 and 180),
    location_reference text,
    location_validated_at timestamptz not null,
    details jsonb not null check (jsonb_typeof(details) = 'object'),
    profile_status varchar(20) not null default 'active'
        check (profile_status in ('active', 'inactive')),
    observed_availability varchar(20) not null default 'unknown'
        check (observed_availability in ('available', 'unavailable', 'unknown')),
    availability_observed_at timestamptz,
    agent_status varchar(20) not null default 'pending'
        check (agent_status in ('pending', 'active', 'failed')),
    agent_id uuid,
    version integer not null default 1 check (version > 0),
    created_by uuid not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (not (details ? 'operating_hours')
        or jsonb_typeof(details->'operating_hours') = 'array'),
    check (not (details ? 'restrictions')
        or jsonb_typeof(details->'restrictions') = 'array'),
    check (agent_status <> 'active' or agent_id is not null),
    check (observed_availability = 'unknown'
        or availability_observed_at is not null)
);

create unique index resources_provider_external_identifier_uidx
    on c03_resource_management.resources
    (provider_id, lower(btrim(external_identifier)));
create index resources_provider_type_idx
    on c03_resource_management.resources (provider_id, resource_type);
create index resources_provider_status_idx
    on c03_resource_management.resources
    (provider_id, profile_status, agent_status);

create function c03_resource_management.resources_set_update_metadata()
returns trigger
language plpgsql
as $$
begin
    new.id := old.id;
    new.created_at := old.created_at;
    new.created_by := old.created_by;
    new.updated_at := clock_timestamp();
    new.version := old.version + 1;
    return new;
end;
$$;

create trigger resources_set_update_metadata
before update on c03_resource_management.resources
for each row execute function
    c03_resource_management.resources_set_update_metadata();

create table c03_resource_management.command_receipts (
    provider_id uuid not null,
    principal_id uuid not null,
    operation text not null,
    idempotency_key varchar(200) not null,
    request_hash varchar(64) not null,
    response_status integer not null,
    response_body jsonb not null,
    created_at timestamptz not null default now(),
    primary key (provider_id, principal_id, operation, idempotency_key)
);

create table c03_resource_management.audit_outcomes (
    id uuid primary key,
    provider_id uuid not null,
    principal_id uuid not null,
    operation text not null,
    resource_id uuid not null,
    resource_version integer not null,
    correlation_id uuid not null,
    outcome jsonb not null,
    created_at timestamptz not null default now()
);
create index audit_provider_resource_idx
    on c03_resource_management.audit_outcomes
    (provider_id, resource_id, created_at);

create table c03_resource_management.outbox (
    id uuid primary key,
    provider_id uuid not null,
    event_type text not null,
    schema_version integer not null check (schema_version > 0),
    resource_id uuid not null,
    resource_version integer not null,
    correlation_id uuid not null,
    payload jsonb not null,
    created_at timestamptz not null default now(),
    published_at timestamptz
);
create index outbox_unpublished_idx
    on c03_resource_management.outbox (created_at)
    where published_at is null;

comment on schema c03_resource_management is
    'C03 resource management: profiles, command receipts, audit outcomes, and outbox.';

-- migrate:down
drop schema if exists c03_resource_management cascade;
