import { createFileRoute } from "@tanstack/react-router";

import { ScreenPlaceholder } from "@/components/layout/screen-placeholder";

export const Route = createFileRoute("/deliveries")({
  component: () => (
    <ScreenPlaceholder
      title="Deliveries"
      description="Delivery requests with their constraints and progress."
      component="C04 Delivery Management"
      owners="Julian + Jolin"
      planned={[
        "Submit a delivery request (UC02)",
        "Validation errors next to each field",
        "Track progress",
        "Filter by status",
      ]}
    />
  ),
});
