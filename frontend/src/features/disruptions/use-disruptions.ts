import { useQuery } from "@tanstack/react-query";

import { useAuth } from "@/features/auth/auth-context";
import { LIVE_REFRESH_MS } from "@/lib/query-client";

import { DisruptionsError, fetchDisruptions, type Disruption } from "./disruptions";

interface Denied {
  denied: "signin" | "forbidden";
}

/**
 * Polls C07 for one run's disruptions. Like the map's snapshot, a 401 or 403
 * replaces the cached list, so a later 503 can't show it again; any other
 * failure keeps the last list, marked as last known.
 */
export function useDisruptions(runId: string) {
  const { request, session } = useAuth();
  const query = useQuery<Disruption[] | Denied>({
    queryKey: ["disruptions", runId, session?.user.id],
    enabled: Boolean(request),
    refetchInterval: LIVE_REFRESH_MS,
    retry: false,
    queryFn: async ({ signal }) => {
      if (!request) return { denied: "signin" };
      try {
        return await fetchDisruptions({ request, runId, signal });
      } catch (error) {
        if (
          error instanceof DisruptionsError &&
          (error.kind === "signin" || error.kind === "forbidden")
        ) {
          return { denied: error.kind };
        }
        throw error;
      }
    },
  });

  const { data, error } = query;
  const denied = data && "denied" in data ? data.denied : null;
  const disruptions = data && !("denied" in data) ? data : undefined;
  const errorKind = error instanceof DisruptionsError ? error.kind : error ? "failed" : denied;
  return {
    disruptions,
    errorKind,
    lastKnown: Boolean(disruptions && error),
    isPending: query.isPending && Boolean(request),
    updatedAt: query.dataUpdatedAt,
  };
}
