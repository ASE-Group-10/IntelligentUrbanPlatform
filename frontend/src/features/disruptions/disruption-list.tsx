import { Link } from "@tanstack/react-router";
import { MapPin, TriangleAlert } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuth } from "@/features/auth/auth-context";
import { SignInForm } from "@/features/auth/resource-access";
import { DEMO_SEGMENTS } from "@/features/city-monitoring/demo-segments";

import type { AffectedPlan, Disruption } from "./disruptions";
import { useDisruptions } from "./use-disruptions";

const time = new Intl.DateTimeFormat("en-IE", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "Europe/Dublin",
});

const SEVERITY = {
  high: { label: "High", className: "bg-status-closed/15 text-status-closed" },
  medium: { label: "Medium", className: "bg-status-slow/15 text-status-slow" },
  low: { label: "Low", className: "bg-muted text-muted-foreground" },
} as const;

const STATUS = { active: "Active", resolved: "Resolved", unknown: "Status unknown" } as const;

const CAUSE: Record<Disruption["cause"], string> = {
  traffic: "Traffic",
  roadworks: "Roadworks",
  event: "Event",
  weather: "Weather",
  incident: "Incident",
  manual: "Manual report",
  resource: "Resource unavailable",
  operational: "Operational issue",
};

/** What C05 did with the replan request C07 sent for one plan. */
function replanText(plan: AffectedPlan): string {
  if (!plan.replanRequested) return "Replan pending: C05 hasn’t taken the request yet";
  switch (plan.replanOutcome) {
    case "PlanSaved":
      return "Revised plan saved";
    case "ReviewRequired":
      return "Revised plan needs operator review";
    case "NoChange":
      return "No change needed";
    default:
      return "Replan requested, waiting for C05";
  }
}

const ERROR_TEXT = {
  signin: "Your session has ended. Sign in again to see disruptions.",
  forbidden: "You don’t have access to this run’s disruptions.",
  unavailable: "Disruptions are unavailable right now. Retrying every few seconds.",
  failed: "Couldn’t read disruptions.",
} as const;

/** Disruptions C07 recorded in one run, with the replans they triggered (#103). */
export function DisruptionList({ runId, runLabel }: { runId: string; runLabel: string }) {
  const auth = useAuth();
  if (!auth.client) {
    return <Notice>Disruptions sign-in is unavailable in this environment.</Notice>;
  }
  if (auth.session === undefined) return <Notice>Checking your session…</Notice>;
  if (!auth.session) return <SignInForm area="Disruptions" />;
  return <SignedInList runId={runId} runLabel={runLabel} />;
}

function SignedInList({ runId, runLabel }: { runId: string; runLabel: string }) {
  const { disruptions, errorKind, lastKnown, isPending, updatedAt } = useDisruptions(runId);

  return (
    <div className="space-y-4">
      {errorKind && (
        <p role="alert" className="text-sm text-destructive">
          {ERROR_TEXT[errorKind]}
        </p>
      )}
      {isPending && <p className="text-sm text-muted-foreground">Loading disruptions…</p>}
      {disruptions && (
        <p className="text-sm text-muted-foreground">
          {lastKnown ? "Last known: " : ""}
          {disruptions.length === 1 ? "1 disruption" : `${disruptions.length} disruptions`} in{" "}
          {runLabel} · checked {time.format(updatedAt)}
        </p>
      )}
      {disruptions?.length === 0 && (
        <Card>
          <CardContent className="text-sm text-muted-foreground">
            No disruptions yet. When a closure hits a planned route, it shows up here with the
            replans it triggered.
          </CardContent>
        </Card>
      )}
      {disruptions?.map((disruption) => (
        <DisruptionCard key={disruption.disruptionId} disruption={disruption} />
      ))}
    </div>
  );
}

function DisruptionCard({ disruption }: { disruption: Disruption }) {
  const severity = SEVERITY[disruption.severity];
  const roads = disruption.segmentKeys.map((key) => ({
    key,
    segment: DEMO_SEGMENTS.find((segment) => segment.segmentKey === key),
  }));
  const where =
    roads.map(({ key, segment }) => segment?.name ?? key).join(", ") || "No road segment";
  // keys are only worth repeating when the title shows street names
  const named = roads.some(({ segment, key }) => segment && segment.name !== key);
  const detected = time.format(new Date(disruption.createdAt));
  const updated = time.format(new Date(disruption.updatedAt));

  return (
    <Card>
      <section aria-label={`${CAUSE[disruption.cause]} on ${where}`}>
        <CardHeader className="gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <Badge className={severity.className}>
              <TriangleAlert aria-hidden="true" />
              {severity.label} severity
            </Badge>
            <Badge variant="outline">{STATUS[disruption.status]}</Badge>
          </div>
          <CardTitle className="text-base">
            {CAUSE[disruption.cause]} on {where}
          </CardTitle>
          <p className="text-xs text-muted-foreground">
            Detected {detected}
            {updated !== detected && ` · updated ${updated}`}
            {named && ` · ${disruption.segmentKeys.join(", ")}`}
          </p>
          {disruption.segmentKeys.length > 0 && (
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
              {roads.map(({ key, segment }) =>
                segment ? (
                  <Link
                    key={key}
                    to="/"
                    search={{ segment: key }}
                    className="inline-flex items-center gap-1 text-primary underline-offset-4 hover:underline"
                  >
                    <MapPin className="size-3.5" aria-hidden="true" />
                    Show {segment.name} on the map
                  </Link>
                ) : (
                  <span key={key} className="text-muted-foreground">
                    {key} · Map unavailable
                  </span>
                ),
              )}
            </div>
          )}
          {disruption.quality !== "good" && (
            <p className="text-xs text-status-slow">
              Detected from {disruption.quality} data, so this may be uncertain.
            </p>
          )}
        </CardHeader>
        <CardContent>
          <h3 className="mb-2 text-sm font-medium">
            Affected plans ({disruption.affectedPlans.length})
          </h3>
          {disruption.affectedPlans.length === 0 ? (
            <p className="text-sm text-muted-foreground">No plans affected.</p>
          ) : (
            <ul className="divide-y rounded-lg border text-sm">
              {disruption.affectedPlans.map((plan) => (
                <li
                  key={plan.planId}
                  className="flex flex-wrap items-center justify-between gap-2 px-3 py-2"
                >
                  <span>
                    Plan <span className="font-mono text-xs">{plan.planId.slice(0, 8)}</span> ·
                    version {plan.planVersion}
                  </span>
                  <span
                    className={
                      plan.replanOutcome === "PlanSaved"
                        ? "text-status-open"
                        : "text-muted-foreground"
                    }
                  >
                    {replanText(plan)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </section>
    </Card>
  );
}

function Notice({ children }: { children: React.ReactNode }) {
  return <div className="p-6 text-sm text-muted-foreground">{children}</div>;
}
