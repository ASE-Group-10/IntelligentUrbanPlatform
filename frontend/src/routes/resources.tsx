import { createFileRoute } from "@tanstack/react-router";

import { ScreenPlaceholder } from "@/components/layout/screen-placeholder";

export const Route = createFileRoute("/resources")({
  component: () => (
    <ScreenPlaceholder
      title="Resources"
      description="Vans, cargo bikes, depots and lockers available to the platform."
      component="C03 Resource Management"
      owners="Julian + Jolin"
      planned={[
        "Register a resource (UC01)",
        "Edit details and availability",
        "See eligibility and agent activation",
        "Provider-scoped list",
      ]}
    />
  ),
});
