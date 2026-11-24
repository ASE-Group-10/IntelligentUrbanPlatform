-- migrate:up
-- HTTP deliveries have no import fingerprint. A matching fingerprint permits
-- the CLI to replay its own input without overwriting subsequently linked data.
alter table c04_delivery_management.deliveries add column initial_snapshot_hash bytea;

do $$
begin
    if to_regnamespace('c04_previous_demo') is null then
        return;
    end if;

    if exists (
        select from c04_previous_demo.deliveries s
          join c04_delivery_management.deliveries d
            on d.scope_key = 'simulation:' || s.run_id::text and d.delivery_id = s.delivery_id
         where d.provider_id <> '07580262-aebe-57c8-84d4-49d45935ce3e'
            or d.created_by <> 'cdff712f-a004-5818-ac58-c50d19694ec1'
    ) or exists (
        select from c04_previous_demo.planning_receipts s
          join c04_delivery_management.command_receipts r
            on r.scope_key = 'simulation:' || s.run_id::text
           and r.principal_id = '00000000-0000-4000-8000-000000000005'
           and r.operation = 'C04.markRequestsPlanned'
           and r.idempotency_key = s.idempotency_key
         where r.response_body <> s.receipt
    ) then
        raise exception 'An existing C04 delivery or planning receipt conflicts with a saved import';
    end if;

    -- The previous schema was populated exclusively by the demo importer.
    -- These fixture IDs carry no C02 memberships or privileges.
    insert into c04_delivery_management.deliveries
        (scope_key, scope, run_id, delivery_id, provider_id, created_by, version,
         status, plan_id, planned_at, delivery, created_at, updated_at, initial_snapshot_hash)
    select 'simulation:' || run_id, 'simulation', run_id, delivery_id,
           '07580262-aebe-57c8-84d4-49d45935ce3e'::uuid,
           'cdff712f-a004-5818-ac58-c50d19694ec1'::uuid,
           (delivery->>'version')::integer, delivery->>'status', plan_id,
           (delivery->>'plannedAt')::timestamptz, delivery,
           (delivery->>'createdAt')::timestamptz, (delivery->>'updatedAt')::timestamptz,
           initial_snapshot_hash
      from c04_previous_demo.deliveries
    -- Reapplying after a downgrade restores only the fingerprint. C04 may
    -- have recorded newer delivery facts or progress in the meantime.
    on conflict (scope_key, delivery_id) do update
       set initial_snapshot_hash = excluded.initial_snapshot_hash;

    -- C04 hashes sorted, compact JSON. This request shape contains only UUIDs and
    -- integer versions; retain the order of request IDs and sort version-map keys.
    insert into c04_delivery_management.command_receipts
        (scope_key, principal_id, operation, idempotency_key, request_hash,
         response_status, response_body)
    select 'simulation:' || run_id,
           case principal_id when 'service:c05' then '00000000-0000-4000-8000-000000000005'
                else principal_id end,
           'C04.markRequestsPlanned', idempotency_key,
           encode(sha256(convert_to(
               '{"expectedVersions":{' ||
               (select string_agg(to_json(key)::text || ':' || value::text, ','
                                  order by key collate "C")
                  from jsonb_each(request_body->'expectedVersions')) ||
               '},"planId":' || (request_body->'planId')::text ||
               ',"requestIds":' || replace((request_body->'requestIds')::text, ', ', ',') ||
               '}', 'UTF8')), 'hex'),
           200, receipt
      from c04_previous_demo.planning_receipts
    on conflict (scope_key, principal_id, operation, idempotency_key) do nothing;

    -- No CASCADE: unexpected objects or dependants stop the migration and the
    -- transaction rolls back with the legacy rows intact.
    drop table c04_previous_demo.planning_receipts, c04_previous_demo.deliveries;
    drop schema c04_previous_demo;
end;
$$;

-- migrate:down
-- A fingerprint describes the original import, which cannot be recovered
-- from a delivery that has already been confirmed or updated. Stage the
-- fixture rows while the C04 schema still exists. Reapplying restores them;
-- downgrading past C04 restores the original feature schema via 09:59:59.
do $$
begin
    lock table c04_delivery_management.deliveries,
               c04_delivery_management.command_receipts in share row exclusive mode;
    if not exists (
        select from c04_delivery_management.deliveries
         where created_by = 'cdff712f-a004-5818-ac58-c50d19694ec1'
           and provider_id = '07580262-aebe-57c8-84d4-49d45935ce3e'
    ) then
        return;
    end if;

    create schema c04_previous_demo;
    create table c04_previous_demo.deliveries (
        run_id uuid not null,
        delivery jsonb not null,
        delivery_id uuid generated always as ((delivery->>'deliveryId')::uuid) stored,
        plan_id uuid,
        initial_snapshot_hash bytea,
        primary key (run_id, delivery_id),
        check ((delivery->>'version')::integer >= 1)
    );
    insert into c04_previous_demo.deliveries (run_id, delivery, plan_id, initial_snapshot_hash)
    select run_id, delivery, plan_id, initial_snapshot_hash
      from c04_delivery_management.deliveries
     where created_by = 'cdff712f-a004-5818-ac58-c50d19694ec1'
       and provider_id = '07580262-aebe-57c8-84d4-49d45935ce3e';

    create table c04_previous_demo.planning_receipts (
        run_id uuid not null, principal_id text not null, idempotency_key text not null,
        request_body jsonb not null, receipt jsonb not null,
        primary key (run_id, principal_id, idempotency_key)
    );
    -- Linking increments each delivery version exactly once. Preserve the
    -- order of the request IDs when rebuilding the old idempotency payload.
    insert into c04_previous_demo.planning_receipts
    select d.run_id, 'service:c05', r.idempotency_key,
           jsonb_build_object('planId', r.response_body->'planId',
                              'requestIds', r.response_body->'requestIds',
                              'expectedVersions', (
                                  select jsonb_object_agg(key, value::integer - 1)
                                    from jsonb_each_text(r.response_body->'versions')
                              )), r.response_body
      from c04_delivery_management.command_receipts r
      join c04_previous_demo.deliveries d
        on r.scope_key = 'simulation:' || d.run_id::text
       and d.delivery_id = (r.response_body->'requestIds'->>0)::uuid
     where r.operation = 'C04.markRequestsPlanned'
       and r.principal_id = '00000000-0000-4000-8000-000000000005';
end;
$$;
alter table c04_delivery_management.deliveries drop column initial_snapshot_hash;
