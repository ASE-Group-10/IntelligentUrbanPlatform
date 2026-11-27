"""C10 evidence objects in Supabase Storage (#130).

Stores one EvidenceSnapshot as an immutable JSON object under the C10-owned
prefix ``c10/evidence/<decisionId>/<snapshotId>.json`` and reads it back. Uploads
never overwrite (``x-upsert: false``): decision-time evidence is immutable.
recordDecision uploads every snapshot it records when this client is configured;
Postgres stays the source of truth.
"""

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import quote
from uuid import UUID

from app.components.c10_decision_records.store import StoreUnavailableError

PREFIX = "c10/evidence"
TIMEOUT_SECONDS = 10

type Opener = Callable[..., Any]


def object_key(decision_id: UUID, snapshot_id: UUID) -> str:
    return f"{PREFIX}/{decision_id}/{snapshot_id}.json"


class _DuplicateError(Exception):
    """The object already exists."""


class SupabaseEvidenceStorage:
    """Supabase Storage REST client for C10's private evidence bucket."""

    def __init__(
        self,
        project_url: str,
        service_key: str,
        bucket: str,
        opener: Opener = urllib.request.urlopen,
    ) -> None:
        # Storage needs a JWT in `authorization`: without it 400 "must have required
        # property 'authorization'", with an sb_secret_ key 403 "Invalid Compact JWS".
        if not service_key.startswith("eyJ"):
            raise ValueError(
                "SUPABASE_SERVICE_KEY must be the legacy service_role key (a JWT "
                "starting with eyJ); Storage rejects sb_ keys in Authorization"
            )
        self._base = f"{project_url.rstrip('/')}/storage/v1/object"
        self._bucket = quote(bucket, safe="")
        self._headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
        }
        self._open = opener

    def put_snapshot(self, decision_id: UUID, snapshot: dict[str, Any]) -> str:
        """Upload one snapshot; return its object key. Never overwrites."""
        key = object_key(decision_id, UUID(snapshot["snapshotId"]))
        body = json.dumps(snapshot, sort_keys=True).encode()
        try:
            self._send(
                "POST",
                f"{self._base}/{self._bucket}/{quote(key)}",
                body,
                {"Content-Type": "application/json", "x-upsert": "false"},
            )
        except _DuplicateError:
            pass  # a retry of the same decision: the immutable object is there
        return key

    def get_snapshot(self, key: str) -> dict[str, Any]:
        """Read a stored snapshot back from the private bucket."""
        url = f"{self._base}/authenticated/{self._bucket}/{quote(key)}"
        return json.loads(self._send("GET", url))

    def _send(
        self, method: str, url: str, body: bytes | None = None, headers=None
    ) -> bytes:
        request = urllib.request.Request(
            url, data=body, method=method, headers={**self._headers, **(headers or {})}
        )
        try:
            with self._open(request, timeout=TIMEOUT_SECONDS) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            # The response body may echo the request, never the key, so it is safe.
            detail = exc.read().decode(errors="replace")[:200]
            if method == "POST" and (exc.code == 409 or "Duplicate" in detail):
                raise _DuplicateError from exc  # Storage may answer 400 with a 409 body
            raise StoreUnavailableError(
                f"Supabase Storage {method} {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise StoreUnavailableError(f"Supabase Storage unreachable: {exc}") from exc


def from_settings(settings) -> SupabaseEvidenceStorage | None:
    """The configured client, or ``None`` when storage is not configured."""
    key = getattr(settings, "supabase_service_key", None)
    url = getattr(settings, "supabase_url", None)
    secret = key.get_secret_value() if key is not None else ""
    if url is None or not secret:  # an empty .env value is unset
        return None
    bucket = getattr(settings, "c10_evidence_bucket", "c10-evidence")
    return SupabaseEvidenceStorage(str(url), secret, bucket)
