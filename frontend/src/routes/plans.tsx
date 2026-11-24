import { createFileRoute } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import scenario from "../../../fixtures/dublin-scenario.json";
import { PageHeader } from "@/components/layout/page-header";
import { AssignmentList } from "@/features/assignments/assignment-list";
import { useAuth } from "@/features/auth/auth-context";
import {
  ProviderPicker,
  ResourceAccess,
  type ResourceScope,
} from "@/features/auth/resource-access";
import { PlanConfirmation } from "@/features/planning/plan-confirmation";
import { plansSearch, proposalLink, type ProposalLink } from "@/features/planning/proposal";

export const Route = createFileRoute("/plans")({
  validateSearch: (search) => plansSearch.parse(search),
  component: Plans,
});

function Plans() {
  const { planId, scope, runId, correlationId } = Route.useSearch();
  const link = useMemo(
    () => proposalLink.safeParse({ planId, scope, runId, correlationId }).data,
    [planId, scope, runId, correlationId],
  );
  const { actingProvider, identity } = useAuth();
  const operator =
    identity?.platformRoles.includes("admin") ||
    identity?.memberships.some(
      (member) =>
        member.providerId === actingProvider?.providerId && member.roles.includes("operator"),
    );
  return (
    <div className="space-y-6">
      <PageHeader
        title="Plans"
        description="Confirm delivery plans, review allocated vehicles and respond to driver offers."
        actions={actingProvider && <ProviderPicker />}
      />
      <ResourceAccess area="Plans">
        {(scope) => (
          <PlansWorkspace
            key={link?.runId ?? ""}
            scope={scope}
            link={link}
            operator={Boolean(operator)}
            invalidLink={planId !== undefined && !link}
          />
        )}
      </ResourceAccess>
    </div>
  );
}

function PlansWorkspace({
  scope,
  link,
  operator,
  invalidLink,
}: {
  scope: ResourceScope;
  link?: ProposalLink;
  operator: boolean;
  invalidLink: boolean;
}) {
  const [runId, setRunId] = useState(link?.runId ?? "");
  return (
    <div className="space-y-4">
      <label className="flex items-center gap-2 text-sm">
        {link ? "Plan and assignment scope" : "Assignment scope"}
        <select
          className="rounded-lg border border-input bg-background px-2 py-1"
          value={runId}
          onChange={(event) => setRunId(event.target.value)}
        >
          <option value="">Live</option>
          <option value={scenario.runId}>Dublin simulation</option>
          {link && link.runId !== scenario.runId && (
            <option value={link.runId}>Simulation {link.runId}</option>
          )}
        </select>
      </label>
      {invalidLink && (
        <p role="alert">
          This proposal link is incomplete. Open the link with the plan, simulation run and original
          delivery trace.
        </p>
      )}
      {link &&
        link.runId === runId &&
        (operator ? (
          <PlanConfirmation key={`${link.planId}:${link.correlationId}`} {...scope} link={link} />
        ) : (
          <p role="alert">Operator access is required to review this proposal.</p>
        ))}
      <AssignmentList key={runId} {...scope} runId={runId} />
    </div>
  );
}
