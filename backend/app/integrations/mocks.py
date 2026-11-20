from typing import Literal, Protocol
from uuid import UUID, uuid5

from app.infrastructure.errors import APIError

LOCATIONS = [
    {"address": "Dublin Depot (synthetic)", "latitude": 53.3498, "longitude": -6.2603},
    {"address": "Docklands Hub (synthetic)", "latitude": 53.348, "longitude": -6.238},
    {
        "address": "City Centre Locker (synthetic)",
        "latitude": 53.3438,
        "longitude": -6.2546,
    },
]
LocationOutcome = Literal["matched", "unmatched", "unconfirmed"]
AgentOutcome = Literal["active", "failed", "pending"]


class LocationGateway(Protocol):
    def validate(self, address: str, latitude: float, longitude: float) -> dict: ...


class AgentGateway(Protocol):
    def activate(self, resource_id: UUID, version: int) -> dict: ...


class MockLocationGateway:
    def __init__(self, outcome: LocationOutcome = "matched"):
        self.outcome = outcome

    def validate(self, address: str, latitude: float, longitude: float) -> dict:
        if self.outcome == "unconfirmed":
            raise APIError(503, "LOCATION_UNCONFIRMED", "Mock mapping is unavailable.")
        for index, location in enumerate(LOCATIONS):
            if (
                self.outcome == "matched"
                and address.casefold() == location["address"].casefold()
                and abs(latitude - location["latitude"]) < 0.0001
                and abs(longitude - location["longitude"]) < 0.0001
            ):
                return {**location, "location_reference": f"mock:dublin:{index}"}
        raise APIError(
            422, "LOCATION_UNMATCHED", "Choose a documented synthetic Dublin location."
        )


class MockAgentGateway:
    """Pure local substitute: no network I/O or real agent is started.

    Real C08 activation must consume the durable request outside this transaction.
    """

    def __init__(self, outcome: AgentOutcome = "active"):
        self.outcome = outcome

    def activate(self, resource_id: UUID, version: int) -> dict:
        return {
            "agent_status": self.outcome,
            "agent_id": uuid5(resource_id, "resource-agent")
            if self.outcome == "active"
            else None,
        }
