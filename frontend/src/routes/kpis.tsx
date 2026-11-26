import { createFileRoute } from "@tanstack/react-router";

import { AnalyticsWorkspace } from "@/features/analytics/workspace";
import { ResourceAccess } from "@/features/auth/resource-access";

export const Route = createFileRoute("/kpis")({
  component: () => (
    <ResourceAccess area="Analytics">{(scope) => <AnalyticsWorkspace {...scope} />}</ResourceAccess>
  ),
});
