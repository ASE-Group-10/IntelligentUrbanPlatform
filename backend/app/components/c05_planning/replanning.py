"""The in-process C05 planning port used by the C07 worker in the Dublin slice."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.components.c06_city_monitoring.ingestion import CityMonitoring
from app.components.c06_city_monitoring.postgres_store import PostgresCityStateStore
from app.components.c07_disruption_detection.contracts import (
    AffectedWork,
    ChangeScope,
    ReplanOutcome,
    ReplanRequest,
)
from app.components.c09_logistics_knowledge.demo_corpus import DEMO_ZONES
from app.components.c09_logistics_knowledge.postgres_store import PostgresKnowledgeStore
from app.components.c09_logistics_knowledge.service import LogisticsKnowledge
from app.components.c10_decision_records.evidence_storage import (
    SupabaseEvidenceStorage,
)
from app.components.c10_decision_records.postgres_store import PostgresDecisionStore
from app.components.c10_decision_records.service import DecisionRecords
from app.demo.feed import Scenario, load_scenario
from app.shared.result import Result

from . import facts, revision
from .store import PostgresPlanStore


@dataclass
class Replanning:
    store: PostgresPlanStore
    knowledge: LogisticsKnowledge
    decisions: DecisionRecords
    scenario: Scenario
    clock: Callable[[], datetime]
    city: CityMonitoring

    def find_affected_work(self, scope: ChangeScope) -> Result[AffectedWork]:
        return facts.find_affected_work(
            self.store, scope, start=scope.from_ or self.clock()
        )

    def request_replan(
        self, request: ReplanRequest, correlation_id: UUID
    ) -> Result[ReplanOutcome]:
        return revision.request_replan(
            self.store,
            self.knowledge,
            self.decisions,
            self.scenario,
            self.clock,
            self.city,
            request,
            correlation_id,
        )


def make_replanning(
    conninfo: str,
    *,
    clock: Callable[[], datetime] | None = None,
    evidence: SupabaseEvidenceStorage | None = None,
) -> Replanning:
    """Build the same PostgreSQL-backed C06, C09 and C10 services used by the API."""
    now = clock or (lambda: datetime.now(UTC))
    return Replanning(
        PostgresPlanStore(conninfo),
        LogisticsKnowledge(PostgresKnowledgeStore(conninfo, DEMO_ZONES), clock=now),
        DecisionRecords(PostgresDecisionStore(), clock=now, evidence=evidence),
        load_scenario(),
        now,
        CityMonitoring(PostgresCityStateStore(conninfo), clock=now),
    )
