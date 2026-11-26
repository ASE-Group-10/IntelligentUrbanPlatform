"""Local walking-skeleton HTTP checks for hops 9/10; never invent identity."""

import http.client
import json
from datetime import timedelta
from urllib.parse import urlencode, urlparse
from uuid import uuid4


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class LocalAPI:
    """Keep the supplied C02 token on the local API, including on redirects."""

    def __init__(self, url, token, provider_id, correlation_id):
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"localhost", "127.0.0.1", "api"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("HTTP verification requires a local API origin")
        if not token or any(c in token for c in "\r\n"):
            raise ValueError("Set ANALYTICS_DEMO_TOKEN to a local C02 analyst token")
        self.origin = parsed
        self.headers = {
            "Authorization": f"Bearer {token}",
            "X-Acting-Provider": str(provider_id),
            "X-Correlation-ID": str(correlation_id),
            "Content-Type": "application/json",
            # The API's TrustedHostMiddleware permits localhost, including when
            # the verifier connects via the explicitly allowed Compose DNS name.
            "Host": "localhost",
        }

    def __call__(self, method, path, body=None, key=None):
        connection_type = (
            http.client.HTTPSConnection
            if self.origin.scheme == "https"
            else http.client.HTTPConnection
        )
        conn = connection_type(self.origin.hostname, self.origin.port, timeout=10)
        headers = dict(self.headers)
        if key:
            headers["Idempotency-Key"] = key
        try:
            conn.request(
                method, path, json.dumps(body) if body is not None else None, headers
            )
            response = conn.getresponse()
            # http.client does not follow redirects or forward credentials.
            require(
                200 <= response.status < 300,
                f"HTTP verification failed: {method} {path.split('?')[0]} "
                f"returned {response.status}",
            )
            require(
                response.getheader("X-Correlation-ID")
                == self.headers["X-Correlation-ID"],
                "API did not preserve the walking-skeleton correlationId",
            )
            data = response.read(2_000_001)
            require(len(data) <= 2_000_000, "HTTP verification response exceeded limit")
            return json.loads(data)
        finally:
            conn.close()


def verify_http(call, provider_id, run_id, events, now, period=None):
    """Fail if the C01-facing HTTP operations cannot see the same saved run.

    The caller supplies an authenticated transport. CI can supply a TestClient
    with an explicitly declared C02 double; that is not sign-in/deployment proof.
    """
    period = period or {
        "start": (now - timedelta(minutes=2)).isoformat(),
        "end": (now + timedelta(minutes=1)).isoformat(),
    }
    query = {
        "scope": {
            "scope": "simulation",
            "providerId": str(provider_id),
            "runId": str(run_id),
            "period": period,
        },
        "metricDefinitionVersion": "unconfigured-v1",
    }
    metrics = call("POST", "/api/v1/analytics/kpis/query", query)
    require(
        metrics["status"] == "partial"
        and metrics["indicators"]
        and all(
            item["value"] is None and item["status"] == "unavailable"
            for item in metrics["indicators"]
        ),
        "Expected explicitly unconfigured indicators, not fabricated KPI values",
    )
    breakdown = call(
        "POST", "/api/v1/analytics/kpis/delivery-time/breakdown?limit=100", query
    )
    require(
        set(breakdown["sourceEventIds"]) == {str(e.event_id) for e in events},
        "C11 HTTP did not expose exactly the published source observations",
    )
    params = urlencode(
        {
            "providerId": str(provider_id),
            "scenarioId": "partial-c04-capture",
            "start": period["start"],
            "end": period["end"],
            "limit": 100,
        }
    )
    cursor = None
    for _ in range(10):
        page = call(
            "GET",
            "/api/v1/simulation-runs?"
            + params
            + ("&" + urlencode({"cursor": cursor}) if cursor else ""),
        )
        manifest = next((i for i in page["items"] if i["runId"] == str(run_id)), None)
        if manifest:
            break
        cursor = page.get("nextCursor")
        if not cursor:
            break
    require(
        manifest is not None and manifest["status"] == "incomplete",
        "C12 HTTP did not list the explicitly incomplete C04-only recording",
    )
    body = {"runId": str(run_id), "timeRange": period, "speed": 1}
    key = str(uuid4())
    frame = call("POST", "/api/v1/replay-sessions", body, key)
    require("sessionId" in frame, "C12 could not open the saved recording")
    session_path = "/api/v1/replay-sessions/" + frame["sessionId"]
    require(
        call("POST", "/api/v1/replay-sessions", body, key) == frame,
        "C12 replay creation retry was not idempotent",
    )
    require(
        call("GET", session_path) == frame == call("GET", session_path),
        "Reading a replay frame must not move its cursor",
    )
    frame = call(
        "POST",
        session_path + "/controls",
        {"command": "play", "expectedCursor": frame["cursor"]},
        str(uuid4()),
    )
    for sequence, event in enumerate(events, 1):
        frame = call(
            "POST", session_path + "/advance", {"expectedCursor": frame["cursor"]}
        )
        require(
            frame["cursor"] == sequence
            and frame["status"] == "playing"
            and frame["state"]["deliveries"][event.payload["deliveryId"]]
            == event.payload["delivery"],
            "C12 playback did not restore the original recorded delivery outcome",
        )
    frame = call("POST", session_path + "/advance", {"expectedCursor": frame["cursor"]})
    require(
        frame["status"] == "completed" and frame["cursor"] == len(events),
        "C12 playback did not finish at the saved recording boundary",
    )
    require(
        call("GET", session_path) == frame,
        "C12 final frame did not persist",
    )
    return {
        "sessionId": frame["sessionId"],
        "cursor": frame["cursor"],
        "status": frame["status"],
        "sourceEventIds": breakdown["sourceEventIds"],
    }
