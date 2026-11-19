# C11 runtime increment — #16

`KPIAnalytics` in `query.py` provides in-process `query_kpis` and
`get_kpi_breakdown`. Every call invokes the injected access policy before reading
the source or cache. The source must return the exact requested provider, period,
area, resources, live/simulation run and definition version. Invalid source data
is rejected through the existing Pydantic contracts.

A successful refresh retains a defensive copy in a bounded process-local cache.
Only `Unavailable` permits fallback: the response is stale with a safe failure
message, new response time and original indicator timestamps/values. Permission,
validation and other failures do not return cached data. A later successful
refresh clears the failure. Eviction/restart loses fallback; no durable storage
is claimed. Refreshes are serialised inside this single service instance.

Breakdowns enforce strict limits 1–100 (default 20), at most 1000 unique source
event IDs, and matching scope/version/metric. Oversized pages fail without silent
truncation. The source adapter owns cursor validation and complete page-local
provenance; this increment cannot verify provenance membership from arbitrary
producer records whose schemas are still unagreed.

Repeatable behaviour demonstration, from `backend/`:

```sh
uv run pytest tests/test_c11_query.py -v
```

The tests use labelled synthetic contract examples and a C02 policy double.
There is no production AuthContext, HTTP endpoint, approved metric formula,
operational-event consumer/deduplication, persistent projection, database
migration, real producer connection or C01 dashboard here. Consequently #16
remains open. Next integration work must supply C02 policy, versioned formulas,
source events/receipts, persistent read models and the HTTP adapter; demonstrate
these against actual collaborators before claiming the thin slice complete.

Prepared by Jolin with AI assistance. README pair Jolin + Qingyuan; no completed
paired session is claimed. Outside reviewer requested: Oscar/C08 or Bruno/C06.
No other component's data, contracts, dependencies or shared schema are changed.
