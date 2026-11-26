"""C11 behaviour against a labelled synthetic read source, not real producers."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.components.c11_kpi_analytics.contracts import Breakdown, KPIQuery, KPISet
from app.components.c11_kpi_analytics.query import KPIAnalytics
from app.shared.result import Failure, Ok

NOW = datetime(2026, 11, 19, 12, tzinfo=UTC)


class SyntheticSource:
    def __init__(self, query, value, page):
        self.value = Ok(value)
        self.page = Ok(page)
        self.calls = []

    def query(self, query):
        self.calls.append(query)
        return self.value

    def breakdown(self, query, metric_id, limit, cursor):
        self.calls.append((query, metric_id, limit, cursor))
        return self.page


@pytest.fixture
def setup(example):
    query = KPIQuery.model_validate(example("query-kpis-request.json"))
    value = KPISet.model_validate(example("kpi-set-partial.json"))
    page = Breakdown.model_validate(example("kpi-breakdown.json"))
    source = SyntheticSource(query, value, page)

    def policy(principal, scope):
        return principal == "analyst"  # labelled C02 double

    service = KPIAnalytics(source, policy, lambda: NOW)
    return service, source, query


def test_refresh_stale_retry_and_defensive_copy(setup):
    service, source, query = setup
    first = service.query_kpis("analyst", query)
    assert isinstance(first, Ok)
    original = first.value.model_dump()
    first.value.indicators[0].reason = "caller mutation"
    source.value = Failure("Unavailable", "private connection details")
    stale = service.query_kpis("analyst", query)
    assert isinstance(stale, Ok)
    assert stale.value.status == "stale"
    assert stale.value.indicators == KPISet.model_validate(original).indicators
    assert "private" not in stale.value.failure
    source.value = Ok(KPISet.model_validate(original))
    retry = service.query_kpis("analyst", query)
    assert retry.value.status == "partial"
    assert retry.value.failure is None


def test_denial_checks_before_source_and_cached_fallback(setup):
    service, source, query = setup
    service.query_kpis("analyst", query)
    count = len(source.calls)
    assert service.query_kpis("outsider", query).kind == "Forbidden"
    assert service.get_kpi_breakdown("outsider", query, "cost").kind == "Forbidden"
    assert len(source.calls) == count


def test_unavailable_without_cache_and_non_transient_failure(setup):
    service, source, query = setup
    good = source.value
    source.value = Failure("Unavailable", "offline")
    assert service.query_kpis("analyst", query).kind == "Unavailable"
    source.value = good
    service.query_kpis("analyst", query)
    source.value = Failure("Forbidden", "revoked")
    assert service.query_kpis("analyst", query).kind == "Forbidden"


@pytest.mark.parametrize("field", ["provider_id", "run_id", "period", "version"])
def test_cache_never_crosses_scopes_or_definition_versions(setup, field):
    service, source, query = setup
    service.query_kpis("analyst", query)
    source.value = Failure("Unavailable", "offline")
    other = query.model_copy(deep=True)
    if field == "version":
        other.metric_definition_version = "other-version"
    elif field == "period":
        other.scope.period.end += timedelta(hours=1)
    else:
        setattr(other.scope, field, uuid4())
    assert service.query_kpis("analyst", other).kind == "Unavailable"


def test_live_and_simulation_cache_are_separate(setup):
    service, source, query = setup
    service.query_kpis("analyst", query)
    source.value = Failure("Unavailable", "offline")
    other = query.model_copy(deep=True)
    other.scope.scope, other.scope.run_id = "live", None
    assert service.query_kpis("analyst", other).kind == "Unavailable"


def test_source_cannot_return_other_provider_or_invalid_value(setup):
    service, source, query = setup
    source.value.value.scope.provider_id = uuid4()
    assert service.query_kpis("analyst", query).kind == "Invalid"
    source.value.value.indicators[0].status = "unavailable"
    source.value.value.indicators[0].value = 0
    assert service.query_kpis("analyst", query).kind == "Invalid"


def test_measured_zero_and_missing_value_survive_refresh(setup):
    service, source, query = setup
    usable = source.value.value.indicators[0]
    usable.value = 0
    result = service.query_kpis("analyst", query).value
    assert result.indicators[0].value == 0
    assert any(i.value is None for i in result.indicators)


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
def test_page_limit_is_strict_and_bounded(setup, limit):
    service, source, query = setup
    assert service.get_kpi_breakdown("analyst", query, "cost", limit).kind == "Invalid"
    assert not source.calls


def test_breakdown_validates_count_provenance_and_scope(setup):
    service, source, query = setup
    page = source.page.value
    metric = page.metric_id
    assert isinstance(service.get_kpi_breakdown("analyst", query, metric), Ok)
    page.records = [{}] * 21
    assert service.get_kpi_breakdown("analyst", query, metric).kind == "Invalid"
    page.records = []
    page.source_event_ids = [uuid4()] * 2
    assert service.get_kpi_breakdown("analyst", query, metric).kind == "Invalid"
    page.source_event_ids = [uuid4() for _ in range(1001)]
    assert service.get_kpi_breakdown("analyst", query, metric).kind == "Invalid"
    page.source_event_ids = []
    page.scope.provider_id = uuid4()
    assert service.get_kpi_breakdown("analyst", query, metric).kind == "Invalid"


def test_cache_eviction_is_bounded(setup):
    _, source, query = setup
    service = KPIAnalytics(source, lambda *_: True, lambda: NOW, 1)
    service.query_kpis("analyst", query)
    other = query.model_copy(deep=True)
    other.metric_definition_version = "other"
    source.value.value.metric_definition_version = "other"
    assert isinstance(service.query_kpis("analyst", other), Ok)
    source.value = Failure("Unavailable", "offline")
    assert service.query_kpis("analyst", query).kind == "Unavailable"
