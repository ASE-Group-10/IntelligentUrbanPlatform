-- migrate:up
create table c05_planning.replan_receipts (
    scope_key text not null,
    plan_id uuid not null,
    base_version integer not null,
    disruption_id uuid not null,
    disruption_version integer not null check (disruption_version >= 1),
    trigger jsonb not null,
    correlation_id uuid not null,
    outcome jsonb not null,
    event_id uuid references c05_planning.outbox (event_id),
    primary key (scope_key, plan_id, disruption_id, disruption_version, base_version),
    foreign key (scope_key, plan_id, base_version)
        references c05_planning.plan_versions (scope_key, plan_id, version),
    check ((outcome ->> 'status' = 'PlanSaved') = (event_id is not null))
);

-- A retry after C07 rediscovers the plan can name a newer base version.
create unique index replan_saved_once on c05_planning.replan_receipts
    (scope_key, plan_id, disruption_id, disruption_version) where event_id is not null;

-- migrate:down
drop table c05_planning.replan_receipts;
