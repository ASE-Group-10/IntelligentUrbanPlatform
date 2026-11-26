-- migrate:up
CREATE SCHEMA c12_simulation_replay;
CREATE TABLE c12_simulation_replay.runs (
    run_id uuid PRIMARY KEY,
    provider_id uuid NOT NULL,
    archive jsonb NOT NULL
);
CREATE TABLE c12_simulation_replay.sessions (
    session_id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    run_id uuid NOT NULL REFERENCES c12_simulation_replay.runs(run_id),
    body jsonb NOT NULL
);
CREATE TABLE c12_simulation_replay.command_receipts (
    provider_id uuid NOT NULL,
    owner_id uuid NOT NULL,
    operation varchar(64) NOT NULL,
    key varchar(200) NOT NULL,
    command jsonb NOT NULL,
    result jsonb NOT NULL,
    PRIMARY KEY (provider_id, owner_id, operation, key)
);
CREATE TABLE c12_simulation_replay.run_page_cursors (
    token uuid PRIMARY KEY,
    binding varchar(64) NOT NULL,
    run_ids jsonb NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE TABLE c12_simulation_replay.captures (
    run_id uuid PRIMARY KEY,
    provider_id uuid NOT NULL,
    body jsonb NOT NULL
);
-- migrate:down
DROP TABLE c12_simulation_replay.captures, c12_simulation_replay.run_page_cursors, c12_simulation_replay.command_receipts, c12_simulation_replay.sessions, c12_simulation_replay.runs;
DROP SCHEMA c12_simulation_replay;
