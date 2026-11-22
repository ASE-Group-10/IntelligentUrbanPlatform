import { useQuery } from "@tanstack/react-query";

import { LIVE_REFRESH_MS } from "@/lib/query-client";

import { CitySnapshotError, fetchCitySnapshot } from "./city-snapshot";
import { DEMO_RUN_ID, DEMO_SEGMENTS } from "./demo-segments";

/** Layer that takes clicks; wider than the drawn line so roads are easy to hit. */
export const SEGMENT_HIT_LAYER = "city-segments-hit";

const SEGMENT_KEYS = DEMO_SEGMENTS.map((segment) => segment.segmentKey);

/** Polls C06 for the demo run's segments; layer and panel share this query. */
export function useCitySnapshot(accessToken?: string) {
  return useQuery({
    queryKey: ["city-snapshot", DEMO_RUN_ID, SEGMENT_KEYS, accessToken],
    queryFn: ({ signal }) =>
      fetchCitySnapshot({ segmentKeys: SEGMENT_KEYS, runId: DEMO_RUN_ID, accessToken, signal }),
    refetchInterval: LIVE_REFRESH_MS,
    retry: false,
  });
}

/**
 * What the map may show. The open/good fallback for absent segments only holds
 * for a snapshot C06 returned, so there is nothing to draw before the first
 * one. A 401 or 403 drops the previous snapshot; any other failure keeps it as
 * last known.
 */
export function useCityConditions(accessToken?: string) {
  const { data, error, isPending } = useCitySnapshot(accessToken);
  const errorKind = error instanceof CitySnapshotError ? error.kind : error ? "failed" : null;
  const snapshot = errorKind === "signin" || errorKind === "forbidden" ? undefined : data;
  return { snapshot, errorKind, lastKnown: Boolean(snapshot && errorKind), isPending };
}
