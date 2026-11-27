import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState, type ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuth } from "@/features/auth/auth-context";
import type { ResourceScope } from "@/features/auth/resource-access";

import {
  fetchDecision,
  fetchEvidence,
  type Candidate,
  type Decision,
  type DecisionLink,
} from "./decisions";

type Caller = Pick<ResourceScope, "providerId" | "request">;

const time = new Intl.DateTimeFormat("en-IE", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "Europe/Dublin",
});

const TYPE: Record<Decision["decisionType"], string> = {
  PlanGenerated: "Plan generated",
  Replan: "Replan",
  Allocation: "Allocation",
  Negotiation: "Negotiation",
};

const OUTCOME: Record<Decision["attemptOutcome"], string> = {
  Selected: "Option selected",
  Infeasible: "No feasible option",
  DataUnavailable: "Data unavailable",
  NoChange: "No change needed",
  ReviewRequired: "Operator review required",
};

const LINK = "text-primary underline-offset-4 hover:underline";

// Records never change once saved, so what was read stays valid.
const SAVED = { staleTime: Infinity, retry: false } as const;

/** What C10 saved about one decision, or the choice between a plan's decisions. */
export function DecisionView({ link, ...caller }: Caller & { link: DecisionLink }) {
  const user = useAuth().session?.user.id;
  const { data, error, isFetching, refetch } = useQuery({
    ...SAVED,
    // A plan can gain decisions, so which one it has is looked up every time.
    staleTime: "planId" in link ? 0 : Infinity,
    queryKey: ["decision", user, caller.providerId, link],
    queryFn: ({ signal }) => fetchDecision({ ...caller, signal }, link),
  });

  if (error) {
    return (
      <div className="space-y-3">
        <p role="alert" className="text-sm text-destructive">
          {error.message}
        </p>
        <Button variant="outline" disabled={isFetching} onClick={() => void refetch()}>
          Try again
        </Button>
      </div>
    );
  }
  if (!data) return <p role="status">Loading decision…</p>;
  if ("candidates" in data) {
    return <CandidateList candidates={data.candidates} runId={link.runId} />;
  }
  return <DecisionDetails {...caller} decision={data.decision} runId={link.runId} />;
}

function CandidateList({ candidates, runId }: { candidates: Candidate[]; runId: string }) {
  return (
    <section aria-labelledby="candidates-heading" className="space-y-3">
      <h2 id="candidates-heading" className="font-heading text-lg font-semibold">
        This plan has {candidates.length} decisions
      </h2>
      <ul className="divide-y rounded-lg border text-sm">
        {candidates.map(({ decisionId, subjectVersion }) => (
          <li key={decisionId} className="px-3 py-2">
            <Link to="/decisions" search={{ decisionId, runId }} className={LINK}>
              {subjectVersion === null ? "No saved version" : `Plan version ${subjectVersion}`} ·
              decision {decisionId.slice(0, 8)}
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

function DecisionDetails({
  decision,
  runId,
  ...caller
}: Caller & { decision: Decision; runId: string }) {
  const titles = new Map(
    decision.evidenceSnapshots.flatMap(({ items }) =>
      items.map((item) => [item.passageId, `${item.documentTitle} § ${item.sectionId}`]),
    ),
  );
  const missing = new Set(
    decision.evidenceStatus.filter((e) => e.status === "Missing").map((e) => e.refId),
  );
  const evidence = (refIds: string[]) =>
    refIds.map((refId) =>
      missing.has(refId) ? (
        <p key={refId} className="text-xs text-status-slow">
          Evidence {refId.slice(0, 8)} is missing from the record.
        </p>
      ) : (
        <EvidencePassage
          key={refId}
          {...caller}
          runId={runId}
          decisionId={decision.decisionId}
          referenceId={refId}
          title={titles.get(refId) ?? `Evidence ${refId.slice(0, 8)}`}
        />
      ),
    );

  return (
    <article aria-labelledby="decision-heading" className="space-y-4">
      <Card>
        <CardHeader className="gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <Badge>{OUTCOME[decision.attemptOutcome]}</Badge>
            <Badge variant="outline">
              {decision.runScope.mode === "Simulation" ? "Simulation" : "Live"}
            </Badge>
          </div>
          <CardTitle>
            <h2 id="decision-heading">
              {TYPE[decision.decisionType]}
              {decision.subject.version !== null && ` · version ${decision.subject.version}`}
            </h2>
          </CardTitle>
          <p className="text-xs break-words text-muted-foreground">
            Decided by {decision.producer} {time.format(new Date(decision.decidedAt))} · recorded{" "}
            {time.format(new Date(decision.recordedAt))}
          </p>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          <p className="break-words">
            {decision.subject.type} {decision.subject.id}
          </p>
          <p className="break-words">Decision {decision.decisionId}</p>
          {decision.relatedDecisionId && (
            <p>
              <Link
                to="/decisions"
                search={{ decisionId: decision.relatedDecisionId, runId }}
                className={LINK}
              >
                View the decision this one follows
              </Link>
            </p>
          )}
        </CardContent>
      </Card>

      <Section title="Selected option">
        <p>{decision.selectedOption?.summary ?? "No option was selected."}</p>
      </Section>

      <Section title="Reasons">
        <ul className="space-y-3">
          {decision.reasons.map((reason) => (
            <li key={reason.reasonId} className="space-y-1">
              <p>{reason.text}</p>
              {evidence(reason.evidenceRefIds)}
            </li>
          ))}
        </ul>
      </Section>

      <Section title="Options not selected">
        {decision.consideredAlternatives.length === 0 ? (
          <p className="text-muted-foreground">No other options were recorded.</p>
        ) : (
          <ul className="space-y-3">
            {decision.consideredAlternatives.map((option) => (
              <li key={option.optionId} className="space-y-1">
                <p className="font-medium">{option.summary}</p>
                <p>Not selected: {option.rejectedReason}</p>
                {evidence(option.evidenceRefIds)}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Effects effects={decision.predictedEffects} />

      {decision.limitations.length > 0 && (
        <Section title="Limitations">
          <ul className="list-disc pl-5">
            {decision.limitations.map((text) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
        </Section>
      )}
    </article>
  );
}

function Effects({ effects }: { effects: Decision["predictedEffects"] }) {
  const shown = [
    ["Travel time", effects?.etaMin, "min"],
    ["Distance", effects?.distanceKm, "km"],
    ["Cost", effects?.costEur, "EUR"],
    ["CO₂", effects?.co2Kg, "kg"],
  ].filter(([, value]) => value !== null && value !== undefined);
  if (shown.length === 0) return null;
  return (
    <Section title="Predicted effects">
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {shown.map(([label, value, unit]) => (
          <div key={label}>
            <dt className="text-xs text-muted-foreground">{label}</dt>
            <dd className="font-medium">
              {value} {unit}
            </dd>
          </div>
        ))}
      </dl>
    </Section>
  );
}

/** One cited passage; its text is read from C10 when opened. */
function EvidencePassage({
  runId,
  decisionId,
  referenceId,
  title,
  ...caller
}: Caller & { runId: string; decisionId: string; referenceId: string; title: string }) {
  const [open, setOpen] = useState(false);
  const user = useAuth().session?.user.id;
  const { data, error } = useQuery({
    ...SAVED,
    enabled: open,
    queryKey: ["decision-evidence", user, caller.providerId, runId, decisionId, referenceId],
    queryFn: ({ signal }) => fetchEvidence({ ...caller, signal }, runId, decisionId, referenceId),
  });
  const passage = data?.evidence;

  return (
    <div className="space-y-2">
      <button
        type="button"
        className={`text-left text-xs ${LINK}`}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        Evidence: {title}
      </button>
      {open && error && (
        <p role="alert" className="text-xs text-destructive">
          {error.message}
        </p>
      )}
      {open && !data && !error && (
        <p role="status" className="text-xs">
          Loading evidence…
        </p>
      )}
      {open && data && (
        <blockquote className="border-l-2 pl-3 text-sm whitespace-pre-line">
          {passage ? (
            <>
              <p>{passage.content}</p>
              <footer className="text-xs text-muted-foreground">
                {passage.documentTitle}, version {passage.documentVersion}, section{" "}
                {passage.sectionId} · as saved at decision time
              </footer>
            </>
          ) : (
            <p>This evidence is missing from the record.</p>
          )}
        </blockquote>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card>
      <CardContent className="space-y-2 text-sm">
        <h3 className="font-medium">{title}</h3>
        {children}
      </CardContent>
    </Card>
  );
}
