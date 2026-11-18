import { createRouter } from "@tanstack/react-router";

import { queryClient } from "@/lib/query-client";

import { routeTree } from "./routeTree.gen";

export function createAppRouter() {
  return createRouter({
    routeTree,
    context: { queryClient },
    defaultPreload: "intent",
    scrollRestoration: true,
  });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
}
