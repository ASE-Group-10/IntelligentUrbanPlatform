-- migrate:up
CREATE SCHEMA c08_allocation;

CREATE TABLE c08_allocation.driver_offers (
    offer_id uuid PRIMARY KEY,
    assignment_ref uuid NOT NULL,
    provider_id uuid NOT NULL REFERENCES c02_identity_access.providers,
    driver_id uuid NOT NULL REFERENCES c02_identity_access.profiles,
    expires_at timestamptz NOT NULL,
    response text CHECK (response IN ('accept', 'reject')),
    response_key varchar(128),
    response_correlation_id uuid,
    responded_at timestamptz,
    UNIQUE (provider_id, driver_id, response_key),
    CHECK (
        (response IS NULL AND response_key IS NULL AND response_correlation_id IS NULL
            AND responded_at IS NULL)
        OR
        (response IS NOT NULL AND response_key IS NOT NULL
            AND response_correlation_id IS NOT NULL AND responded_at IS NOT NULL)
    )
);

-- migrate:down
DROP TABLE c08_allocation.driver_offers;
DROP SCHEMA c08_allocation;
