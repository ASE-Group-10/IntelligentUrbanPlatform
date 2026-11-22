from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.components.c02_identity_access import records
from app.infrastructure.database import make_engine
from app.main import create_app


def test_driver_offer_response_persists_and_replays(test_database_url):
    url = make_url(test_database_url).set(drivername="postgresql+psycopg")
    engine = make_engine(url.render_as_string(hide_password=False))
    provider, driver, other_driver, operator = (uuid4() for _ in range(4))
    offer, other_offer, expired_offer, assignment = (uuid4() for _ in range(4))
    try:
        with engine.begin() as connection:
            connection.execute(
                records.providers.insert().values(
                    provider_id=provider, provider_name="Demo"
                )
            )
            for user, role in (
                (driver, "driver"),
                (other_driver, "driver"),
                (operator, "operator"),
            ):
                connection.execute(
                    records.profiles.insert().values(
                        principal_id=user, display_name=role
                    )
                )
                connection.execute(
                    records.memberships.insert().values(
                        principal_id=user, provider_id=provider, role=role
                    )
                )
            for offer_id, seconds in (
                (offer, 300),
                (other_offer, 300),
                (expired_offer, -300),
            ):
                connection.execute(
                    text("""
                        INSERT INTO c08_allocation.driver_offers
                            (offer_id, assignment_ref, provider_id, driver_id, expires_at)
                        VALUES (:offer, :assignment, :provider, :driver,
                                now() + make_interval(secs => :seconds))
                    """),
                    {
                        "offer": offer_id,
                        "assignment": assignment,
                        "provider": provider,
                        "driver": driver,
                        "seconds": seconds,
                    },
                )

        app = create_app(
            engine,
            SimpleNamespace(
                mock_dependencies=True,
                supabase_url=None,
                availability_max_age_seconds=86400,
            ),
            identity_verifier=SimpleNamespace(subject=UUID),
        )
        with TestClient(app) as client:
            path = f"/api/v1/driver-offers/{offer}/response"
            headers = {"Authorization": f"Bearer {driver}", "Idempotency-Key": "demo"}
            assert client.post(path, json={"response": "accept"}).status_code == 401
            for user in (other_driver, operator):
                denied = {**headers, "Authorization": f"Bearer {user}"}
                assert (
                    client.post(
                        path, headers=denied, json={"response": "accept"}
                    ).status_code
                    == 403
                )

            first = client.post(path, headers=headers, json={"response": "accept"})
            assert first.status_code == 200, first.text
            assert first.headers["Idempotency-Replayed"] == "false"
            assert first.json()["acceptanceStatus"] == "accepted"
            assert first.json()["assignmentRef"] == str(assignment)
            assert first.json()["correlationId"] == first.headers["X-Correlation-ID"]

            retry = client.post(path, headers=headers, json={"response": "accept"})
            assert retry.headers["Idempotency-Replayed"] == "true"
            assert retry.json() == first.json()
            assert (
                client.post(
                    path, headers=headers, json={"response": "reject"}
                ).status_code
                == 409
            )

            other_path = f"/api/v1/driver-offers/{other_offer}/response"
            assert (
                client.post(
                    other_path, headers=headers, json={"response": "accept"}
                ).status_code
                == 409
            )
            fresh = {**headers, "Idempotency-Key": "fresh"}
            assert (
                client.post(
                    path, headers=fresh, json={"response": "accept"}
                ).status_code
                == 409
            )
            expired_path = f"/api/v1/driver-offers/{expired_offer}/response"
            assert (
                client.post(
                    expired_path, headers=fresh, json={"response": "accept"}
                ).status_code
                == 409
            )
            rejected = client.post(
                other_path, headers=fresh, json={"response": "reject"}
            )
            assert rejected.status_code == 200
            assert rejected.json()["acceptanceStatus"] == "rejected"

        with engine.connect() as connection:
            persisted = dict(
                connection.execute(
                    text("SELECT offer_id, response FROM c08_allocation.driver_offers")
                ).all()
            )
        assert persisted == {
            offer: "accept",
            other_offer: "reject",
            expired_offer: None,
        }
    finally:
        engine.dispose()
