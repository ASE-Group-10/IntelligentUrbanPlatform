"""Read the route and duration supplied by a confirmed, saved C05 revision."""

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo


def matches_event(saved, event):
    payload = event.payload
    scope = {"scope": event.scope}
    if event.run_id:
        scope["runId"] = str(event.run_id)
    return (
        saved["status"] == "confirmed"
        and saved["planId"] == str(payload.plan_id)
        and saved["version"] == payload.plan_version
        and saved["runScope"] == scope
        and saved["decisionId"] == str(payload.decision_id)
        and saved["resourceNeeds"]
        == [
            need.model_dump(mode="json", by_alias=True) for need in payload.requirements
        ]
    )


def route_fits(saved, delivery_id, details, departure):
    routes = [r for r in saved["routes"] if str(delivery_id) in r["deliveryIds"]]
    if len(routes) != 1 or departure is None or departure.utcoffset() is None:
        return False
    route = routes[0]
    distance, duration = route["distanceMetres"], route["estimatedDurationSeconds"]
    if distance < 0 or duration < 0 or (distance > 0 and not route["segmentKeys"]):
        return False
    if details.get("range_km") is not None and distance > details["range_km"] * 1000:
        return False
    # C05's slice has no zone or temperature evidence to verify restrictions.
    if details.get("restrictions"):
        return False
    hours = details.get("operating_hours", [])
    if not hours:
        return True
    zone = ZoneInfo(details["timezone"])
    start = departure.astimezone(zone)
    for slot in hours:
        if slot["day"] != start.strftime("%A").lower():
            continue
        opens = time.fromisoformat(slot["opens"])
        closes = time.fromisoformat(slot["closes"])
        if opens <= start.time() <= closes:
            closing = datetime.combine(start.date(), closes, tzinfo=zone)
            available = closing.astimezone(UTC) - departure.astimezone(UTC)
            if duration <= available.total_seconds():
                return True
    return False
