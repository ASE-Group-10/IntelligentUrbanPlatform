-- migrate:up
create schema if not exists c10_decision_records;
comment on schema c10_decision_records is
    'C10 Decision Records and Explanation (UC10): decisions with their evidence, and audit outcomes.';

-- One row per decision attempt, insert-only. `record` holds the DecisionRecord
-- exactly as received (camelCase contract JSON), including the full content of
-- every evidence snapshot, so explanations never need C09. The other columns are
-- copies used for idempotency, lookups and checks.
create table c10_decision_records.decisions (
    seq bigint generated always as identity unique,
    decision_id uuid primary key,
    idempotency_key text not null unique,
    request_hash text not null,
    producer text not null check (producer in ('C05', 'C08')),
    decision_type text not null,
    subject_type text not null,
    subject_id uuid not null,
    subject_version integer check (subject_version >= 1),
    attempt_outcome text not null,
    run_mode text not null check (run_mode in ('Live', 'Simulation')),
    run_id uuid,
    corpus_version integer,
    decided_at timestamptz not null,
    recorded_at timestamptz not null,
    record jsonb not null,
    check (run_mode <> 'Simulation' or run_id is not null)
);

-- findDecision selects by subject; C12 replay selects by run.
create index decisions_by_subject
    on c10_decision_records.decisions (subject_type, subject_id, subject_version);
create index decisions_by_run
    on c10_decision_records.decisions (run_id)
    where run_id is not null;

-- One row per protected operation, allowed or not (recordOutcome).
create table c10_decision_records.audit_outcomes (
    seq bigint generated always as identity unique,
    audit_id uuid primary key,
    operation text not null,
    outcome text not null,
    principal_id text not null,
    correlation_id uuid not null,
    occurred_at timestamptz not null,
    recorded_at timestamptz not null,
    entry jsonb not null
);

-- Decision-time evidence and audit history must never change after insert.
create function c10_decision_records.reject_change() returns trigger
    language plpgsql as $$
begin
    raise exception 'c10_decision_records.% is insert-only (% rejected)',
        tg_table_name, tg_op;
end;
$$;

create trigger decisions_insert_only
    before update or delete on c10_decision_records.decisions
    for each row execute function c10_decision_records.reject_change();

create trigger audit_outcomes_insert_only
    before update or delete on c10_decision_records.audit_outcomes
    for each row execute function c10_decision_records.reject_change();

-- migrate:down
drop table c10_decision_records.audit_outcomes;
drop table c10_decision_records.decisions;
drop function c10_decision_records.reject_change();
drop schema c10_decision_records;
