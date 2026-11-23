-- migrate:up
-- NULL represents live state; a UUID identifies one simulation run.
ALTER TABLE c08_allocation.demo_plans
    ADD COLUMN run_id uuid,
    DROP CONSTRAINT demo_plans_pkey,
    ADD UNIQUE NULLS NOT DISTINCT (run_id, plan_id);
ALTER TABLE c08_allocation.allocations
    ADD COLUMN run_id uuid,
    DROP CONSTRAINT allocations_plan_id_delivery_id_key,
    ADD UNIQUE NULLS NOT DISTINCT (run_id, plan_id, delivery_id);
ALTER TABLE c08_allocation.command_receipts
    ADD COLUMN run_id uuid,
    DROP CONSTRAINT command_receipts_pkey,
    ADD UNIQUE NULLS NOT DISTINCT (run_id, provider_id, principal_id, operation, key);

-- migrate:down
-- Refuse to reinterpret a simulation as live on rollback.
DO $$
BEGIN
    IF EXISTS (SELECT FROM c08_allocation.demo_plans WHERE run_id IS NOT NULL)
        OR EXISTS (SELECT FROM c08_allocation.allocations WHERE run_id IS NOT NULL)
        OR EXISTS (SELECT FROM c08_allocation.command_receipts WHERE run_id IS NOT NULL)
    THEN
        RAISE EXCEPTION 'Archive simulation data before rolling back C08 run isolation';
    END IF;
END $$;
ALTER TABLE c08_allocation.command_receipts
    DROP COLUMN run_id, ADD PRIMARY KEY (provider_id, principal_id, operation, key);
ALTER TABLE c08_allocation.allocations
    DROP COLUMN run_id, ADD UNIQUE (plan_id, delivery_id);
ALTER TABLE c08_allocation.demo_plans
    DROP COLUMN run_id, ADD PRIMARY KEY (plan_id);
