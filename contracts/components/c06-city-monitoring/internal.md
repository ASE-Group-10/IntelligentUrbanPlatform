# C06 City Monitoring: internal interfaces

C06 owns City State (UC05). This file covers the calls other components make to
C06 inside the backend process. Events are in `events.yaml`. HTTP operations
for C01 and other clients are in `openapi.yaml` (`getCitySnapshot` so far;
`submitManualCondition` will follow).

Typed models: `backend/app/components/c06_city_monitoring/contracts.py` and
`backend/app/shared/contracts.py`. Examples: `examples/`.

## Conventions

These conventions apply to the C06 and C07 contracts. All pairs need to agree
them once (#28).

| Item | Convention |
| --- | --- |
| Identifiers | UUID v4 strings for every entity and event ID (`eventId`, `planId`, `disruptionId`, ...) |
| Versions | Integers starting at 1, incremented on every committed change to an entity |
| Timestamps | ISO 8601 in UTC with `Z` suffix, for example `2026-11-20T08:15:00Z`; the UI converts to local time |
| Road segment key | `seg:<osm_way_id>` (OpenStreetMap way ID), for example `seg:4471023`. **Agreed.** |
| Zone key | `zone:<code>`, using a fixed grid or Dublin postal district (to decide) |
| Units | Speed in km/h, delay in seconds, rainfall in mm/h, temperature in °C, distance in metres |
| Quality | Enum `good` / `degraded` / `unknown` on any observed or derived value |
| Run scope | `live` or `simulation`, plus `runId` when simulation; replay never writes to `live` |
| Failure outcomes | The shared `Result<T>` kinds: `Invalid`, `Forbidden`, `NotFound`, `Conflict`, `Unavailable`, `Unconfirmed`, `Infeasible`, `Partial` |

Where a field is marked *slice: no*, it can be left out until Iteration 3 but
the name is reserved.

## Provided interfaces

### getCitySnapshot(scope, freshnessPolicy) → Result\<CitySnapshot\>

Callers: C05 (planning) and C07 (full context after `CityStateUpdated`).
Examples: `examples/city-snapshot-query.json`, `examples/city-snapshot.json`.

**Request: CitySnapshotQuery**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `scope.segmentKeys` | array of string | one of | Specific segments |
| `scope.bbox` | [minLon, minLat, maxLon, maxLat] | one of | Area query |
| `freshnessPolicy.maxAgeSeconds` | integer | no | Values older than this come back `degraded`; default 900 |
| `atVersion` | integer | no | Read a past snapshot version (used by C10/C12) |

**Response: CitySnapshot**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `snapshotVersion` | integer | yes | Monotonic City State version |
| `generatedAt` | timestamp | yes | When this snapshot was assembled |
| `segments` | array of SegmentCondition | yes | Only segments with a non-default condition; absent = `open`, `good` |
| `staleSources` | array of string | yes | Empty when all feeds are fresh |
| `overallQuality` | enum | yes | Worst quality among returned values |

`SegmentCondition` is defined in `events.yaml` (`components.schemas.SegmentCondition`).

Behaviour (implemented in `backend/app/components/c06_city_monitoring/snapshot.py`):

- The caller also passes the City State scope: live, or one simulation `runId`.
  A run's state is never mixed with live state.
- Requested segments without a recorded condition are omitted (absent means
  `open`, `good`).
- In a current snapshot, a `good` value older than `maxAgeSeconds` (default
  900) is returned as `degraded` and its sources are listed in `staleSources`.
  Past versions (`atVersion`) are returned as they were recorded.
- `overallQuality` is the worst quality among the returned segments, or `good`
  when none are returned.
- `scope.bbox` is rejected as `Invalid` until segments have geometry in C06.
- `scope.segmentKeys` must hold 1 to 200 keys, the same limit as `openapi.yaml`.
- A read has no side effects: it sees committed City State and never writes, so
  an ingestion committed during the read is kept. Returned segments are copies.

**Failures:** `Invalid` (bad scope, `bbox`, or 0 or more than 200 segment keys), `NotFound` (`atVersion` newer
than the current version), `Unavailable` (no City State yet, or store down),
`Forbidden`.

### ingestObservation(sourceContext, observation) → Result\<IngestionReceipt\>

Callers: the traffic, weather and event source adapters, and manual reports.
Examples: `examples/observation-closure.json`, `examples/ingestion-receipt.json`.

**Request: Observation**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `observationId` | UUID | yes | Assigned by source or C06; dedup key |
| `sourceId` | string | yes | For example `sim-traffic`, `manual` |
| `kind` | enum | yes | `traffic` / `weather` / `event` / `closure` |
| `target` | object | yes | `{ "segmentKey" }` or `{ "zoneKey" }` or `{ "lat", "lon" }` (C06 maps to a segment) |
| `values` | object | yes | Raw values in source units, for example `{ "status": "closed" }` |
| `observedAt` | timestamp | yes | Source time; older than current → history only (AF4) |
| `author` / `expiresAt` | string / timestamp | manual only | AF5 |

**Response: IngestionReceipt**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `observationId` | UUID | yes | The ingested observation |
| `disposition` | enum | yes | `merged` / `history_only` / `conflict` / `rejected` |
| `snapshotVersion` | integer | when merged | New City State version |

Re-ingesting the same `observationId` returns the first receipt and changes
nothing. A merged observation commits the City State change and the
`CityStateUpdated` outbox entry in one transaction.

**Failures:** `Invalid` (unusable values or unsupported target), `Unavailable`
(store down; nothing is saved).

Zone-level weather (`zoneKey`, `rainMmH`, `tempC`, `windKmh`, `quality`)
follows the same pattern. *slice: no*.

## Required interfaces

None yet. Source authentication will use C02 once its contract exists.

## Still open

- [ ] RabbitMQ exchange and queue naming, and who creates them (proposed: `ulop.events` topic exchange, routing key = `eventType`, shared setup script in the repo).
- [ ] Where the shared `EventEnvelope` and conventions live (#28).
- [ ] Zones: fixed grid or postal districts? Only needed for weather, so this can wait.
- [ ] Per-source freshness overrides.
- [x] City State read for C01: `getCitySnapshot` in `openapi.yaml`, served by `c06_city_monitoring/router.py`.
- [ ] `submitManualCondition` over HTTP (UC05 AF5).
- [ ] Segment geometry for area (`bbox`) queries and the C01 map (#43): from C05's routing graph or a demo fixture.
