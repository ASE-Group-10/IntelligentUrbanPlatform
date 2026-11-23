import { createFileRoute } from "@tanstack/react-router";

import { PageHeader } from "@/components/layout/page-header";
import { DisruptionList } from "@/features/disruptions/disruption-list";
import { DEMO_RUN_ID } from "@/features/disruptions/disruptions";

export const Route = createFileRoute("/disruptions")({ component: Disruptions });

function Disruptions() {
  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 p-4 sm:p-6">
      <PageHeader
        title="Disruptions"
        description="Road closures that hit planned routes, and the replans they triggered."
      />
      <DisruptionList runId={DEMO_RUN_ID} runLabel="the Dublin simulation" />
    </div>
  );
}
