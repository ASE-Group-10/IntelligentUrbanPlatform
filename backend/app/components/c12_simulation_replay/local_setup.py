"""Provision the restricted LOCAL Compose playback login, never a shared DB.

The entry point uses a fixed loopback connection inside the db container's
network namespace. It does not read DATABASE_URL or MIGRATION_DATABASE_URL.
Credentials here are public, disposable local defaults, like Compose's ulop user.
"""

import os

import psycopg
from psycopg import sql

LOCAL_ROLE = "c12_playback"
LOCAL_PASSWORD = "c12_local_only"
LOCAL_CONNINFO = "postgres://ulop:ulop@127.0.0.1:5432/ulop?sslmode=disable"
ALLOWED = {
    "c12_simulation_replay.runs": {"SELECT"},
    "c12_simulation_replay.sessions": {"SELECT", "INSERT", "UPDATE"},
    "c12_simulation_replay.command_receipts": {"SELECT", "INSERT", "UPDATE"},
    "c12_simulation_replay.run_page_cursors": {"SELECT", "INSERT", "DELETE"},
}
PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
    "MAINTAIN",
)


def provision(conn, *, role=LOCAL_ROLE, expected_database="ulop"):
    """Idempotent setup on the caller's transaction; extra access fails closed.

    The test hook uses a unique role in the existing dedicated ulop_test service.
    Only main() chooses a connection, and its target cannot be configured remotely.
    """
    if expected_database not in {"ulop", "ulop_test"}:
        raise ValueError("Local replay setup requires the local or dedicated test DB")
    if conn.execute("select current_database()").fetchone()[0] != expected_database:
        raise ValueError("Unexpected database for local replay setup")
    # Prevent concurrent Compose one-shots from racing role creation/grants.
    conn.execute("select pg_advisory_xact_lock(12121112)")
    flags = conn.execute(
        "select rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
        "rolbypassrls, rolinherit from pg_roles where rolname = %s",
        (role,),
    ).fetchone()
    if flags is None:
        conn.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS NOINHERIT"
            ).format(sql.Identifier(role))
        )
    elif any(flags):
        raise ValueError("Existing playback role has unsafe role attributes")
    if conn.execute(
        "select 1 from pg_auth_members where member = "
        "(select oid from pg_roles where rolname = %s)",
        (role,),
    ).fetchone():
        raise ValueError("Playback login must not belong to another role")
    conn.execute(
        sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(role),
            sql.Literal(LOCAL_PASSWORD),
        )
    )
    conn.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(expected_database),
            sql.Identifier(role),
        )
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA c12_simulation_replay TO {}").format(
            sql.Identifier(role),
        )
    )
    for table, permissions in ALLOWED.items():
        schema, name = table.split(".")
        conn.execute(
            sql.SQL("GRANT {} ON {}.{} TO {}").format(
                sql.SQL(", ".join(sorted(permissions))),
                sql.Identifier(schema),
                sql.Identifier(name),
                sql.Identifier(role),
            )
        )
    # Catch stale/direct/PUBLIC business grants as well as ownership privileges.
    tables = conn.execute(
        "select n.nspname || '.' || c.relname, c.oid from pg_class c "
        "join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname ~ '^c[0-9]{2}_' and c.relkind in ('r', 'p', 'v', 'm')"
    ).fetchall()
    for table, oid in tables:
        allowed = ALLOWED.get(table, set())
        for privilege in PRIVILEGES:
            if (
                privilege not in allowed
                and conn.execute(
                    "select has_table_privilege(%s, %s, %s)",
                    (role, oid, privilege),
                ).fetchone()[0]
            ):
                raise ValueError("Playback login has unexpected component access")
    if conn.execute(
        "select has_database_privilege(%s, current_database(), 'CREATE')",
        (role,),
    ).fetchone()[0]:
        raise ValueError("Playback login must not create schemas")
    if conn.execute(
        "select 1 from pg_namespace where nspname ~ '^c[0-9]{2}_' "
        "and has_schema_privilege(%s, oid, 'CREATE')",
        (role,),
    ).fetchone():
        raise ValueError("Playback login must not create component objects")


def main():
    if os.environ.get("C12_LOCAL_COMPOSE") != "1":
        raise ValueError("Use the local Compose c12-local-setup service")
    with psycopg.connect(LOCAL_CONNINFO, connect_timeout=5) as conn:
        provision(conn)
    print("Local restricted C12 playback login ready")


if __name__ == "__main__":
    main()
