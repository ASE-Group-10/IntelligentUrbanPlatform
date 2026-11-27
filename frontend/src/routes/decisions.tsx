import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useState, type FormEvent } from "react";
import { z } from "zod";

import scenario from "../../../fixtures/dublin-scenario.json";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/features/auth/auth-context";
import { ProviderPicker, ResourceAccess } from "@/features/auth/resource-access";
import { DecisionView } from "@/features/decisions/decision-view";
import type { DecisionLink } from "@/features/decisions/decisions";

const searchField = z.string().optional().catch("");
const decisionsSearch = z.object({
  decisionId: searchField,
  planId: searchField,
  runId: searchField,
});
const uuid = z.uuid().toLowerCase();

export const Route = createFileRoute("/decisions")({
  validateSearch: (search) => decisionsSearch.parse(search),
  component: Decisions,
});

/** The decision or plan the URL names; the Dublin run unless it names another. */
function decisionLink(search: z.infer<typeof decisionsSearch>): DecisionLink | undefined {
  const runId = uuid.safeParse(search.runId ?? scenario.runId).data;
  const decisionId = uuid.safeParse(search.decisionId).data;
  const planId = uuid.safeParse(search.planId).data;
  if (!runId) return undefined;
  if (search.decisionId !== undefined) return decisionId ? { runId, decisionId } : undefined;
  return planId ? { runId, planId } : undefined;
}

function Decisions() {
  const search = Route.useSearch();
  const link = decisionLink(search);
  const named = search.decisionId !== undefined || search.planId !== undefined;
  const { actingProvider } = useAuth();
  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 p-4 sm:p-6">
      <PageHeader
        title="Decisions"
        description="Why a plan was chosen, with the evidence saved at decision time."
        actions={actingProvider && <ProviderPicker />}
      />
      <ResourceAccess area="Decisions">
        {({ providerId, request }) => (
          <div className="space-y-6">
            <PlanSearch key={search.planId} planId={search.planId} runId={search.runId} />
            {named && !link && (
              <p role="alert">This decision link is incomplete. Check the IDs and try again.</p>
            )}
            {link && (
              <DecisionView
                key={JSON.stringify(link)}
                providerId={providerId}
                request={request}
                link={link}
              />
            )}
          </div>
        )}
      </ResourceAccess>
    </div>
  );
}

function PlanSearch({ planId = "", runId }: { planId?: string; runId?: string }) {
  const navigate = useNavigate();
  const [invalid, setInvalid] = useState(false);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const entered = uuid.safeParse(
      String(new FormData(event.currentTarget).get("planId")).trim(),
    ).data;
    setInvalid(!entered);
    if (entered) void navigate({ to: "/decisions", search: { planId: entered, runId } });
  }

  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-2" noValidate>
      <label className="grid min-w-0 flex-1 gap-1 text-sm">
        Plan ID
        <input
          name="planId"
          defaultValue={planId}
          aria-invalid={invalid}
          className="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
        />
      </label>
      <Button type="submit">Find decisions</Button>
      {invalid && (
        <p role="alert" className="w-full text-sm text-destructive">
          Enter a full plan ID, as shown on the Plans page.
        </p>
      )}
    </form>
  );
}
