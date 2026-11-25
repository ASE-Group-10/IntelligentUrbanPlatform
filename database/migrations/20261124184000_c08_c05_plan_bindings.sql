-- migrate:up
-- A C05-backed binding stores only the C08 vehicle/driver choice. C05 owns
-- its version, capacity requirement, route and departure.
ALTER TABLE c08_allocation.demo_plans
    ALTER COLUMN plan_version DROP NOT NULL,
    ALTER COLUMN required_kg DROP NOT NULL,
    ADD CONSTRAINT plan_binding_source CHECK (
        (plan_version IS NOT NULL AND required_kg IS NOT NULL)
        OR (plan_version IS NULL AND required_kg IS NULL
            AND saved_revision IS NULL AND departure_at IS NULL)
    );

-- migrate:down
-- Existing C05 bindings must be archived before reverting to fixture-only plans.
ALTER TABLE c08_allocation.demo_plans
    ALTER COLUMN plan_version SET NOT NULL,
    ALTER COLUMN required_kg SET NOT NULL,
    DROP CONSTRAINT plan_binding_source;
