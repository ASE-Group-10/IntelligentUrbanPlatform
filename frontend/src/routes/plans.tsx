import { createFileRoute } from "@tanstack/react-router";
import { PageHeader } from "@/components/layout/page-header";
import { AssignmentList } from "@/features/assignments/assignment-list";
import { ResourceAccess } from "@/features/auth/resource-access";

export const Route = createFileRoute("/plans")({
  component: () => (
    <div className="space-y-6">
      <PageHeader
        title="Plans"
        description="Review allocated vehicles and respond to driver offers."
      />
      <ResourceAccess area="Plans">{(scope) => <AssignmentList {...scope} />}</ResourceAccess>
    </div>
  ),
});
