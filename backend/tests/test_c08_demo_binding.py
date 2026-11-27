"""Local demo-bind fixture (hop 6): one provider, driver and eligible van."""

import pytest

from app.components.c08_allocation.store import AllocationStore, has_driver_membership
from app.demo import c08_binding
from app.infrastructure.database import make_engine


def test_requires_the_local_compose_service(monkeypatch, capsys):
    monkeypatch.delenv("C08_LOCAL_DEMO", raising=False)
    with pytest.raises(SystemExit):
        c08_binding.main(["6f777c7d-55ba-46fd-8a7c-60cdd766372b"])
    assert "demo-bind service" in capsys.readouterr().err


def test_fixture_is_idempotent_and_bindable(test_database_url):
    engine = make_engine(
        test_database_url.replace("postgres://", "postgresql+psycopg://").replace(
            "postgresql://", "postgresql+psycopg://"
        )
    )
    try:
        van = c08_binding.seed_fixture(engine, 86400)
        assert c08_binding.seed_fixture(engine, 86400) == van
        store = AllocationStore(engine, 86400)
        with engine.begin() as conn:
            vehicle = store.vehicle(conn, c08_binding.PROVIDER_ID, van)
            assert vehicle is not None
            assert vehicle["details"]["vehicle_category"] == "van"
            assert has_driver_membership(
                conn, c08_binding.PROVIDER_ID, c08_binding.DRIVER_ID
            )
    finally:
        engine.dispose()
