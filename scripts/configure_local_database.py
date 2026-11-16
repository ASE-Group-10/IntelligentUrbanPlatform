"""Create one restricted local backend login using the already logged-in CLI.

Run after migrations and supabase link. Secrets never go to stdout or Git.
Existing .env files are preserved. This is explicit developer setup, not app startup.
"""

import os
import secrets
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote, urlparse

import psycopg

ROOT = Path(__file__).resolve().parents[1]


def main():
    destination = ROOT / "backend/.env"
    if destination.exists():
        raise SystemExit(
            "backend/.env already exists; kept the existing configuration."
        )
    pooler = ROOT / "supabase/.temp/pooler-url"
    project = ROOT / "supabase/.temp/project-ref"
    if not pooler.exists() or not project.exists():
        raise SystemExit("Run supabase link <project-ref> first.")
    project_ref = project.read_text().strip()
    parsed = urlparse(pooler.read_text().strip())
    role = "c03_local_" + secrets.token_hex(4)
    password = secrets.token_urlsafe(36)
    sql = (
        f"CREATE ROLE {role} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
        f"NOREPLICATION PASSWORD '{password}'; GRANT c03_backend TO {role};"
    )
    filename = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as file:
            filename = file.name
            os.chmod(filename, 0o600)
            file.write("BEGIN; " + sql + " COMMIT;")
        result = subprocess.run(
            [
                "supabase",
                "db",
                "query",
                f"--remote={project_ref}",
                "--file",
                filename,
                "--yes",
                "--output",
                "json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise SystemExit(
                "Could not create the login. Check migrations and CLI access."
            )
    finally:
        if filename:
            Path(filename).unlink(missing_ok=True)
    host = parsed.hostname
    port = parsed.port or 5432
    plain_url = (
        f"postgresql://{role}.{project_ref}:{quote(password, safe='')}"
        f"@{host}:{port}/postgres?sslmode=require"
    )
    # Save before testing so credentials remain recoverable on a transient outage.
    sqlalchemy_url = plain_url.replace("postgresql://", "postgresql+psycopg://", 1)
    content = (
        "# Local development only. Never commit this file.\n"
        f"DATABASE_URL={sqlalchemy_url}\n"
        "MOCK_DEPENDENCIES=true\nAVAILABILITY_MAX_AGE_SECONDS=86400\n"
    )
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(content)
    try:
        with psycopg.connect(plain_url, connect_timeout=10) as connection:
            connection.execute("SELECT 1")
    except psycopg.Error:
        raise SystemExit(
            "Configuration saved; connection check failed. Retry startup."
        ) from None
    print("Backend connection verified; credentials saved in ignored backend/.env.")


if __name__ == "__main__":
    main()
