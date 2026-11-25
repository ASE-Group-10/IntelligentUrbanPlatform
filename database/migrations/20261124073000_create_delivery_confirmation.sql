-- migrate:up
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
