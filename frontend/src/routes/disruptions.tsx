import { createFileRoute } from "@tanstack/react-router";

import { ScreenPlaceholder } from "@/components/layout/screen-placeholder";

export const Route = createFileRoute("/disruptions")({
  component: () => (
    <ScreenPlaceholder
      title="Disruptions"
      description="Detected disruptions and the replanning they triggered."
      component="C07 Disruption Detection"
      owners="Bruno + Alejandro"
      planned={[
        "Active disruptions with severity",
        "Affected plans and replan outcome",
        "Link from a closure on the map",
        "Revised plan side by side",
      ]}
    />
  ),
});
