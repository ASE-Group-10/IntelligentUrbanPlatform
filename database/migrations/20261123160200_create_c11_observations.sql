-- migrate:up
CREATE SCHEMA c11_kpi_analytics;
CREATE TABLE c11_kpi_analytics.observations (
    event_id uuid PRIMARY KEY,
    digest varchar(64) NOT NULL,
    scope_key varchar(50) NOT NULL,
    delivery_id uuid NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    occurred_at timestamptz NOT NULL,
    facts jsonb NOT NULL,
    received_at timestamptz NOT NULL
);
CREATE INDEX c11_observation_scope ON c11_kpi_analytics.observations(scope_key, delivery_id, occurred_at, event_id);
CREATE TABLE c11_kpi_analytics.delivery_bindings (
    scope_key varchar(50) NOT NULL,
    delivery_id uuid NOT NULL,
    provider_id uuid NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (scope_key, delivery_id)
);
CREATE TABLE c11_kpi_analytics.page_cursors (
    token uuid PRIMARY KEY,
    binding varchar(64) NOT NULL,
    "offset" integer NOT NULL CHECK ("offset" >= 0),
    expires_at timestamptz NOT NULL,
    as_of timestamptz NOT NULL
);
-- migrate:down
DROP TABLE c11_kpi_analytics.page_cursors, c11_kpi_analytics.delivery_bindings, c11_kpi_analytics.observations;
DROP SCHEMA c11_kpi_analytics;
