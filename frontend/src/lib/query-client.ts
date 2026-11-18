import { QueryClient } from "@tanstack/react-query";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      retry: 1,
      refetchOnWindowFocus: true,
    },
  },
});

/** Live views (City State, disruptions, plans) poll at this interval. */
export const LIVE_REFRESH_MS = 5_000;
