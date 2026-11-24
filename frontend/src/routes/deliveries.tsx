import { createFileRoute } from "@tanstack/react-router";

import { ResourceAccess } from "@/features/auth/resource-access";
import { DeliveryWorkspace } from "@/features/deliveries/delivery-workspace";

function DeliveriesPage() {
  return (
    <ResourceAccess area="Deliveries">{(scope) => <DeliveryWorkspace {...scope} />}</ResourceAccess>
  );
}

export const Route = createFileRoute("/deliveries")({
  component: DeliveriesPage,
});
