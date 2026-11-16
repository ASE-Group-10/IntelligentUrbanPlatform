-- Baseline of the existing UC01 table on 16 November 2026.
-- Hours, timezone, and restrictions live inside details.
-- UC01: Resource profiles. Type-specific details remain JSONB.
-- Target: IntelligentUrbanPlatform (gfjqwlkclokbycdantrl).
-- Provider/principal/agent UUIDs deliberately have no foreign keys until contracts exist.
-- Location validation and typed JSONB validation are performed by C03 before insert.
BEGIN;

CREATE TABLE public.resources (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id uuid NOT NULL,
    external_identifier varchar(100) NOT NULL CHECK (btrim(external_identifier) <> ''),
    name varchar(200) NOT NULL CHECK (btrim(name) <> ''),
    resource_type varchar(20) NOT NULL
        CHECK (resource_type IN ('vehicle', 'depot', 'warehouse', 'locker')),
    address text NOT NULL CHECK (btrim(address) <> ''),
    latitude numeric(9,6) NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude numeric(9,6) NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    location_reference text,
    location_validated_at timestamptz NOT NULL,
    details jsonb NOT NULL CHECK (jsonb_typeof(details) = 'object'),
    CONSTRAINT resources_details_operating_hours_array
        CHECK (NOT (details ? 'operating_hours') OR jsonb_typeof(details->'operating_hours') = 'array'),
    CONSTRAINT resources_details_restrictions_array
        CHECK (NOT (details ? 'restrictions') OR jsonb_typeof(details->'restrictions') = 'array'),
    profile_status varchar(20) NOT NULL DEFAULT 'active'
        CHECK (profile_status IN ('active', 'inactive')),
    observed_availability varchar(20) NOT NULL DEFAULT 'unknown'
        CHECK (observed_availability IN ('available', 'unavailable', 'unknown')),
    availability_observed_at timestamptz,
    agent_status varchar(20) NOT NULL DEFAULT 'pending'
        CHECK (agent_status IN ('pending', 'active', 'failed')),
    agent_id uuid,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT resources_active_agent_has_id
        CHECK (agent_status <> 'active' OR agent_id IS NOT NULL),
    CONSTRAINT resources_known_availability_has_timestamp
        CHECK (observed_availability = 'unknown' OR availability_observed_at IS NOT NULL)
);

-- Resource identifiers are case-insensitive within each provider.
CREATE UNIQUE INDEX resources_provider_external_identifier_uidx
    ON public.resources (provider_id, lower(btrim(external_identifier)));
CREATE INDEX resources_provider_type_idx
    ON public.resources (provider_id, resource_type);
CREATE INDEX resources_provider_status_idx
    ON public.resources (provider_id, profile_status, agent_status);

CREATE FUNCTION public.resources_set_update_metadata()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $$
BEGIN
    NEW.id := OLD.id;
    NEW.created_at := OLD.created_at;
    NEW.created_by := OLD.created_by;
    NEW.updated_at := clock_timestamp();
    NEW.version := OLD.version + 1;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.resources_set_update_metadata() FROM PUBLIC;

CREATE TRIGGER resources_set_update_metadata
BEFORE UPDATE ON public.resources
FOR EACH ROW EXECUTE FUNCTION public.resources_set_update_metadata();

ALTER TABLE public.resources ENABLE ROW LEVEL SECURITY;
-- No browser policies until trusted provider membership/roles are agreed.
REVOKE ALL ON TABLE public.resources FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON TABLE public.resources TO service_role;

COMMENT ON TABLE public.resources IS
    'C03 resource profiles with typed-by-application JSONB details. No pending review: validate before insert. C08 owns committed capacity and allocations. Backend access only until C02 policies exist.';
COMMENT ON COLUMN public.resources.details IS
    'Resource-type attributes plus timezone, operating_hours, and restrictions. C03 validates complete typed contents. Empty hours mean unspecified, not unrestricted or 24/7.';
COMMENT ON COLUMN public.resources.version IS
    'Automatically incremented on update. C03 must use WHERE id = :id AND version = :expected_version and handle zero rows as a conflict.';
COMMENT ON COLUMN public.resources.observed_availability IS
    'Observed status only; does not represent capacity reservations or allocation commitments.';

COMMIT;
