"""HTTP fixture that generates a real plan from a saved Dublin delivery."""

from dataclasses import dataclass
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.components.c04_delivery_management.planning import seed_demo_delivery
from app.components.c05_planning.models import Plan, PlanScope, SavedDelivery
from app.components.c09_logistics_knowledge.demo_corpus import load
from app.config import Settings
from app.main import create_app
from app.shared.result import Ok


@dataclass
class Flow:
    client: TestClient
    plan: Plan
    trace: UUID
    headers: dict[str, str]

    def confirm(self, **changes):
        body = {
            "expectedVersion": self.plan.version,
            "runScope": self.plan.run_scope.model_dump(mode="json", by_alias=True),
        }
        return self.client.post(
            f"/api/v1/plans/{self.plan.plan_id}/confirmation",
            headers=self.headers,
            json={**body, **changes},
        )


@pytest.fixture
def flow(example, test_database_url):
    run_id, trace, operator, provider = (uuid4() for _ in range(4))
    saved = example("delivery-submitted.json")
    saved["deliveryId"] = str(uuid4())
    with psycopg.connect(test_database_url) as conn:
        seed_demo_delivery(conn, run_id, saved)
        conn.execute(
            "insert into c02_identity_access.profiles "
            "values (%s, 'test operator', false)",
            (operator,),
        )
        conn.execute(
            "insert into c02_identity_access.providers values (%s, 'test provider')",
            (provider,),
        )
        conn.execute(
            "insert into c02_identity_access.provider_memberships "
            "values (%s, %s, 'operator')",
            (operator, provider),
        )

    class TestVerifier:
        def subject(self, token):
            assert token == "operator-session"
            return operator

    config = Settings(
        database_url="postgresql+psycopg://" + test_database_url.split("://", 1)[1],
        SHARED_RUN_IDS={run_id},
    )
    with TestClient(
        create_app(settings=config, identity_verifier=TestVerifier())
    ) as client:
        assert all(
            isinstance(result, Ok) for result in load(client.app.state.knowledge)
        )
        result = client.app.state.planning.generate_plan(
            SavedDelivery.model_validate(saved),
            PlanScope(scope="simulation", run_id=run_id),
            trace,
            "from-c04",
        )
        assert isinstance(result, Ok), result
        headers = {
            "Authorization": "Bearer operator-session",
            "Idempotency-Key": "operator-click",
            "X-Correlation-ID": str(trace),
            "X-Acting-Provider": str(provider),
        }
        yield Flow(client, result.value, trace, headers)
