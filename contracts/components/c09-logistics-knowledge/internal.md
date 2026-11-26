# C09 Logistics Knowledge: internal interfaces

C09 owns the curated policy corpus and knowledge retrieval (UC09). The HTTP
operations used by the browser (C01: curator publishing, operator lookups) are in
`openapi.yaml`. This file covers the calls other components make inside the
backend process.

Typed models: `backend/app/components/c09_logistics_knowledge/contracts.py`;
service: `backend/app/components/c09_logistics_knowledge/service.py`.
Conventions and `AuthContext`: see `../c10-decision-records/internal.md`.
Draft v0.1, owned by Qingyuan + Bangchen (#13).

## Provided interfaces

### queryKnowledge(ctx, query: PolicyQuery) → Result\<KnowledgeResult\>

Called by **C05** before generating or replanning a plan, and by **C08** when
evaluating agent proposals. It is the same operation as HTTP
`POST /api/v1/knowledge/query`; request and response are the `C09PolicyQuery`
and `C09KnowledgeResult` schemas in `openapi.yaml`, with its examples.

- Read-only; no idempotency key. Roles: `Service:C05`, `Service:C08`.
- Returns applicable passages of the active corpus, ranked by similarity to
  `freeText` (all score 1.0 without it).
- **Every applicable passage that carries constraints is always returned** and
  checked for conflicts; `maxPassages` only limits rule-free passages.
  `ruleCoverage: Incomplete` (explained in `limitations`) means not all hard rules
  could be returned: UC03 returns `DataUnavailable` and UC07 `ReviewRequired`
  without saving a revision. C10 rejects a `Selected` decision built on it.
- **Rule scope:** a constraint applies inside its `zoneIds`; when `segmentKeys`
  is non-empty, only on those C06 segments. C05 matches them against the route
  and stop segments, so a detour off those segments is not bound by the rule.
  Required fields per kind: `VehicleBan` value null; `LoadingWindow` needs
  `timeOfDay`, value null; `MaxGrossWeight` needs `value: {"maxKg": >0}`.
- Provenance (`corpusVersion`, `indexVersion`, `embeddingModel`, `retrievedAt`,
  `ruleCoverage`, `conflicts`, `limitations`) is repeated on the result for
  convenience; it must equal the `evidenceSnapshot`, which is the record.
- Same request on the same `corpusVersion` gives the same order.
- `status`: `Ok`, `LowConfidence`, `Conflict` or `NoApplicable`. None of these
  is a failure; record `limitations` in the decision.
- Pass `evidenceSnapshot` **unchanged** to C10 `recordDecision`.
- Callers time out after 5 s and treat that as `Unavailable`.

**Failures:** `Invalid` (`C09.INVALID_TIME_WINDOW`, `C09.UNKNOWN_ZONE`),
`Forbidden` (`C09.FORBIDDEN`), `Unavailable` (`C09.INDEX_UNAVAILABLE`,
`C09.EMBEDDING_UNAVAILABLE`): plan with hard constraints only and record the
limitation `knowledge unavailable`.

## Required interfaces

None. Curator conflict warnings (`notifyKnowledgeConflict`) are used only inside
C09 and are not a contract.
