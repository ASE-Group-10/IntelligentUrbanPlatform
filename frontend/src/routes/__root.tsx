import type { QueryClient } from "@tanstack/react-query";
import { createRootRouteWithContext, Link, Outlet } from "@tanstack/react-router";

import { AppShell } from "@/components/layout/app-shell";
import { Button } from "@/components/ui/button";
import { Toaster } from "@/components/ui/sonner";

interface RouterContext {
  queryClient: QueryClient;
}

export const Route = createRootRouteWithContext<RouterContext>()({
  component: RootLayout,
  notFoundComponent: NotFound,
});

function RootLayout() {
  return (
    <AppShell>
      <Outlet />
      <Toaster richColors closeButton />
    </AppShell>
  );
}

function NotFound() {
  return (
    <div className="mx-auto flex max-w-md flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
      <p className="font-mono text-sm text-muted-foreground">404</p>
      <h1 className="font-heading text-xl font-semibold">This page does not exist</h1>
      <p className="text-sm text-muted-foreground">
        Check the address, or go back to the city map.
      </p>
      <Button render={<Link to="/" />}>Open the city map</Button>
    </div>
  );
}
