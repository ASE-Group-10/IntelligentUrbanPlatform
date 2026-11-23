-- migrate:up
CREATE SCHEMA c08_allocation;

-- Trusted, live-only fixture for hop 6 until C05 supplies its saved-plan lookup.
CREATE TABLE c08_allocation.demo_plans (
    plan_id uuid PRIMARY KEY,
    plan_version integer NOT NULL CHECK (plan_version > 0),
    delivery_id uuid NOT NULL,
    provider_id uuid NOT NULL,
    resource_id uuid NOT NULL,
    driver_id uuid NOT NULL,
    required_kg numeric NOT NULL CHECK (required_kg > 0)
);

CREATE TABLE c08_allocation.processed_events (
    event_id uuid PRIMARY KEY,
    correlation_id uuid NOT NULL
);

CREATE TABLE c08_allocation.allocations (
    allocation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    assignment_ref uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    event_id uuid NOT NULL UNIQUE REFERENCES c08_allocation.processed_events,
    correlation_id uuid NOT NULL,
    plan_id uuid NOT NULL,
    plan_version integer NOT NULL,
    delivery_id uuid NOT NULL,
    provider_id uuid NOT NULL,
    resource_id uuid NOT NULL,
    resource_name text NOT NULL,
    resource_version integer NOT NULL,
    driver_id uuid NOT NULL,
    capacity_kg numeric NOT NULL CHECK (capacity_kg > 0),
    status text NOT NULL DEFAULT 'allocated'
        CHECK (status IN ('allocated','offered','accepted','rejected')),
    offer_id uuid UNIQUE,
    UNIQUE (plan_id, delivery_id),
    CHECK ((status = 'allocated') = (offer_id IS NULL))
);
CREATE INDEX allocations_resource ON c08_allocation.allocations(resource_id);
CREATE INDEX allocations_provider ON c08_allocation.allocations(provider_id, driver_id);

CREATE TABLE c08_allocation.command_receipts (
    provider_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    operation text NOT NULL,
    key text NOT NULL,
    command jsonb NOT NULL,
    outcome jsonb NOT NULL,
    PRIMARY KEY (provider_id, principal_id, operation, key)
);

-- migrate:down
DROP TABLE c08_allocation.command_receipts, c08_allocation.allocations;
DROP TABLE c08_allocation.processed_events, c08_allocation.demo_plans;
DROP SCHEMA c08_allocation;
