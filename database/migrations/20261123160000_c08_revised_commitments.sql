-- migrate:up
ALTER TABLE c08_allocation.demo_plans
    ADD COLUMN saved_revision jsonb,
    ADD COLUMN departure_at timestamptz;

ALTER TABLE c08_allocation.allocations
    DROP CONSTRAINT allocations_status_check,
    DROP CONSTRAINT allocations_check,
    ADD CONSTRAINT allocations_status_check
        CHECK (status IN ('allocated','offered','accepted','rejected','unresolved')),
    ADD CONSTRAINT allocations_check
        CHECK (status = 'unresolved' OR ((status = 'allocated') = (offer_id IS NULL)));

-- migrate:down
-- The original checks reject rollback while any unresolved assignment remains.
ALTER TABLE c08_allocation.allocations
    DROP CONSTRAINT allocations_status_check,
    DROP CONSTRAINT allocations_check,
    ADD CONSTRAINT allocations_status_check
        CHECK (status IN ('allocated','offered','accepted','rejected')),
    ADD CONSTRAINT allocations_check CHECK ((status = 'allocated') = (offer_id IS NULL));
ALTER TABLE c08_allocation.demo_plans DROP COLUMN saved_revision, DROP COLUMN departure_at;
