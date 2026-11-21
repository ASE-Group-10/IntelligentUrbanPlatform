-- migrate:up
CREATE SCHEMA c02_identity_access;
CREATE TABLE c02_identity_access.profiles (
    principal_id uuid PRIMARY KEY,
    display_name text NOT NULL,
    platform_admin boolean NOT NULL DEFAULT false
);
CREATE TABLE c02_identity_access.providers (
    provider_id uuid PRIMARY KEY,
    provider_name text NOT NULL
);
CREATE TABLE c02_identity_access.provider_memberships (
    principal_id uuid NOT NULL REFERENCES c02_identity_access.profiles,
    provider_id uuid NOT NULL REFERENCES c02_identity_access.providers,
    role text NOT NULL CHECK (role IN ('manager', 'operator', 'driver', 'analyst', 'curator')),
    PRIMARY KEY (principal_id, provider_id, role)
);

-- migrate:down
DROP TABLE c02_identity_access.provider_memberships;
DROP TABLE c02_identity_access.providers;
DROP TABLE c02_identity_access.profiles;
DROP SCHEMA c02_identity_access;
