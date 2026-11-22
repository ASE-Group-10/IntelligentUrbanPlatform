import { useQuery } from "@tanstack/react-query";

import { LIVE_REFRESH_MS } from "@/lib/query-client";

import { CitySnapshotError, fetchCitySnapshot, type CitySnapshot } from "./city-snapshot";
import { DEMO_RUN_ID, DEMO_SEGMENTS } from "./demo-segments";

/** Layer that takes clicks; wider than the drawn line so roads are easy to hit. */
export const SEGMENT_HIT_LAYER = "city-segments-hit";

const SEGMENT_KEYS = DEMO_SEGMENTS.map((segment) => segment.segmentKey);

/** A 401 or 403: the session may not see conditions, whatever was shown before. */
interface Denied {
  denied: "signin" | "forbidden";
}

/**
 * Polls C06 for the demo run's segments; layer and panel share this query.
 * A 401 or 403 becomes the query's data, so it replaces the previous snapshot
 * in the cache and a later 503 can't bring that snapshot back (#99 review).
 */
export function useCitySnapshot(accessToken?: string) {
  return useQuery<CitySnapshot | Denied>({
    queryKey: ["city-snapshot", DEMO_RUN_ID, SEGMENT_KEYS, accessToken],
    queryFn: async ({ signal }) => {
      try {
        return await fetchCitySnapshot({
          segmentKeys: SEGMENT_KEYS,
          runId: DEMO_RUN_ID,
          accessToken,
          signal,
        });
      } catch (error) {
        if (
          error instanceof CitySnapshotError &&
          (error.kind === "signin" || error.kind === "forbidden")
        ) {
          return { denied: error.kind };
        }
        throw error;
      }
    },
    refetchInterval: LIVE_REFRESH_MS,
    retry: false,
  });
}

/**
 * What the map may show. The open/good fallback for absent segments only holds
 * for a snapshot C06 returned, so there is nothing to draw before the first
 * one. After a 401 or 403 nothing is shown until a request succeeds again;
 * any other failure keeps the last snapshot, marked as last known.
 */
export function useCityConditions(accessToken?: string) {
  const { data, error, isPending } = useCitySnapshot(accessToken);
  const denied = data && "denied" in data ? data.denied : null;
  const snapshot = data && !("denied" in data) ? data : undefined;
  const errorKind = error instanceof CitySnapshotError ? error.kind : error ? "failed" : denied;
  return { snapshot, errorKind, lastKnown: Boolean(snapshot && error), isPending };
}
