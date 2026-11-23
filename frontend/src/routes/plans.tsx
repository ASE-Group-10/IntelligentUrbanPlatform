import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";
import scenario from "../../../fixtures/dublin-scenario.json";
import { PageHeader } from "@/components/layout/page-header";
import { AssignmentList } from "@/features/assignments/assignment-list";
import { useAuth } from "@/features/auth/auth-context";
import { ProviderPicker, ResourceAccess } from "@/features/auth/resource-access";

export const Route = createFileRoute("/plans")({ component: Plans });

function Plans() {
  const [runId, setRunId] = useState("");
  const { actingProvider } = useAuth();
  return (
    <div className="space-y-6">
      <PageHeader
        title="Plans"
        description="Review allocated vehicles and respond to driver offers."
        actions={actingProvider && <ProviderPicker />}
      />
      <ResourceAccess area="Plans">
        {(scope) => (
          <div className="space-y-4">
            <label className="flex items-center gap-2 text-sm">
              Assignment scope
              <select
                className="rounded-lg border border-input bg-background px-2 py-1"
                value={runId}
                onChange={(event) => setRunId(event.target.value)}
              >
                <option value="">Live</option>
                <option value={scenario.runId}>Dublin simulation</option>
              </select>
            </label>
            <AssignmentList key={runId} {...scope} runId={runId} />
          </div>
        )}
      </ResourceAccess>
    </div>
  );
}
