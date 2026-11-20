import { createFileRoute } from "@tanstack/react-router";

import { ScreenPlaceholder } from "@/components/layout/screen-placeholder";

export const Route = createFileRoute("/kpis")({
  component: () => (
    <ScreenPlaceholder
      title="KPIs"
      description="Labelled indicators for the scenario, with units and coverage."
      component="C11 KPI Analytics"
      owners="Jolin (UC11 handover still to be agreed)"
      planned={[
        "On-time rate and distance",
        "Emissions estimate (labelled)",
        "Coverage and timestamps",
        "Charts per run",
      ]}
    />
  ),
});
