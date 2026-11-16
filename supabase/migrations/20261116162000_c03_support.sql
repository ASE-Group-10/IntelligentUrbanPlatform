BEGIN;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'c03_backend') THEN
        CREATE ROLE c03_backend NOLOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA public TO c03_backend;
GRANT SELECT, INSERT, UPDATE ON public.resources TO c03_backend;
CREATE POLICY resources_c03_provider_scope ON public.resources
    FOR ALL TO c03_backend
    USING (provider_id = nullif(current_setting('app.provider_id', true), '')::uuid)
    WITH CHECK (provider_id = nullif(current_setting('app.provider_id', true), '')::uuid);

CREATE TABLE public.c03_command_receipts (
    provider_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    operation text NOT NULL,
    idempotency_key varchar(200) NOT NULL,
    request_hash varchar(64) NOT NULL,
    response_status integer NOT NULL,
    response_body jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider_id, principal_id, operation, idempotency_key)
);

CREATE TABLE public.c03_audit_outcomes (
    id uuid PRIMARY KEY,
    provider_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    operation text NOT NULL,
    resource_id uuid NOT NULL,
    resource_version integer NOT NULL,
    correlation_id uuid NOT NULL,
    outcome jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX c03_audit_provider_resource_idx
    ON public.c03_audit_outcomes(provider_id, resource_id, created_at);

CREATE TABLE public.c03_outbox (
    id uuid PRIMARY KEY,
    provider_id uuid NOT NULL,
    event_type text NOT NULL,
    schema_version integer NOT NULL CHECK (schema_version > 0),
    resource_id uuid NOT NULL,
    resource_version integer NOT NULL,
    correlation_id uuid NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz
);
CREATE INDEX c03_outbox_unpublished_idx ON public.c03_outbox(created_at)
    WHERE published_at IS NULL;

ALTER TABLE public.c03_command_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.c03_audit_outcomes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.c03_outbox ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.c03_command_receipts, public.c03_audit_outcomes, public.c03_outbox
    FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT ON public.c03_command_receipts, public.c03_audit_outcomes,
    public.c03_outbox TO c03_backend;

CREATE POLICY c03_receipts_provider_scope ON public.c03_command_receipts
    FOR ALL TO c03_backend
    USING (provider_id = nullif(current_setting('app.provider_id', true), '')::uuid)
    WITH CHECK (provider_id = nullif(current_setting('app.provider_id', true), '')::uuid);
CREATE POLICY c03_audit_provider_scope ON public.c03_audit_outcomes
    FOR ALL TO c03_backend
    USING (provider_id = nullif(current_setting('app.provider_id', true), '')::uuid)
    WITH CHECK (provider_id = nullif(current_setting('app.provider_id', true), '')::uuid);
CREATE POLICY c03_outbox_provider_scope ON public.c03_outbox
    FOR ALL TO c03_backend
    USING (provider_id = nullif(current_setting('app.provider_id', true), '')::uuid)
    WITH CHECK (provider_id = nullif(current_setting('app.provider_id', true), '')::uuid);

COMMENT ON TABLE public.c03_audit_outcomes IS
    'Development stand-in for C10. Successful command outcomes commit with C03 state; agree C10 integration before replacing.';
COMMENT ON TABLE public.c03_outbox IS
    'Development-scoped events only. No live broker publisher is implemented. Do not replay these into live C08.';
COMMIT;
