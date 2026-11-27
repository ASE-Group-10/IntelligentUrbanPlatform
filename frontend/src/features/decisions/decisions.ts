import { z } from "zod";

import { SessionError, type SessionRequest } from "@/features/auth/identity";

/** C10 reads (contracts/components/c10-decision-records/openapi.yaml); fields shown here only. */
const decisionSchema = z.object({
  decisionId: z.uuid(),
  decisionType: z.enum(["PlanGenerated", "Replan", "Allocation", "Negotiation"]),
  producer: z.string(),
  subject: z.object({ type: z.string(), id: z.uuid(), version: z.number().int().nullable() }),
  attemptOutcome: z.enum([
    "Selected",
    "Infeasible",
    "DataUnavailable",
    "NoChange",
    "ReviewRequired",
  ]),
  relatedDecisionId: z.uuid().nullable(),
  selectedOption: z.object({ optionId: z.string(), summary: z.string() }).nullable(),
  consideredAlternatives: z.array(
    z.object({
      optionId: z.string(),
      summary: z.string(),
      rejectedReason: z.string(),
      evidenceRefIds: z.array(z.uuid()),
    }),
  ),
  reasons: z.array(
    z.object({ reasonId: z.string(), text: z.string(), evidenceRefIds: z.array(z.uuid()) }),
  ),
  predictedEffects: z
    .object({
      etaMin: z.number().nullish(),
      distanceKm: z.number().nullish(),
      costEur: z.number().nullish(),
      co2Kg: z.number().nullish(),
    })
    .nullish(),
  evidenceSnapshots: z.array(
    z.object({
      items: z.array(
        z.object({ passageId: z.uuid(), documentTitle: z.string(), sectionId: z.string() }),
      ),
    }),
  ),
  evidenceStatus: z.array(z.object({ refId: z.uuid(), status: z.enum(["Stored", "Missing"]) })),
  limitations: z.array(z.string()),
  runScope: z.object({ mode: z.enum(["Live", "Simulation"]), runId: z.uuid().nullable() }),
  decidedAt: z.string(),
  recordedAt: z.string(),
});
export type Decision = z.infer<typeof decisionSchema>;

const evidenceSchema = z.object({
  decisionId: z.uuid(),
  referenceId: z.string(),
  evidence: z
    .object({
      content: z.string(),
      documentTitle: z.string(),
      documentVersion: z.number().int(),
      sectionId: z.string(),
    })
    .nullable(),
});
export type Evidence = z.infer<typeof evidenceSchema>;

const ambiguousSchema = z.object({
  error: z.object({
    details: z.object({
      candidates: z
        .array(z.object({ decisionId: z.uuid(), subjectVersion: z.number().int().nullable() }))
        .min(1),
    }),
  }),
});
export type Candidate = z.infer<typeof ambiguousSchema>["error"]["details"]["candidates"][number];

/** One decision in a simulation run, or the decisions about one plan. */
export type DecisionLink = { runId: string } & ({ decisionId: string } | { planId: string });

export class DecisionError extends Error {}

export interface DecisionCaller {
  request: SessionRequest;
  providerId: string;
  signal?: AbortSignal;
}

const MESSAGES: Record<number, string> = {
  401: "Your session has ended. Sign in again to see decisions.",
  403: "You don’t have access to decisions in this run.",
  404: "No decision was found in this run.",
  422: "Check the ID and try again.",
};
const UNAVAILABLE = "Decision records are unavailable right now. Try again shortly.";
const UNREADABLE = "Couldn’t read the decision record.";

async function get(caller: DecisionCaller, path: string): Promise<Response> {
  try {
    return await caller.request(path, {
      headers: { "X-Acting-Provider": caller.providerId },
      signal: caller.signal,
    });
  } catch (error) {
    if (caller.signal?.aborted) throw error;
    throw new DecisionError(error instanceof SessionError ? MESSAGES[401] : UNAVAILABLE);
  }
}

async function read<T>(response: Response, schema: z.ZodType<T>): Promise<T> {
  if (!response.ok) {
    throw new DecisionError(
      MESSAGES[response.status] ?? (response.status >= 500 ? UNAVAILABLE : UNREADABLE),
    );
  }
  const parsed = schema.safeParse(await response.json().catch(() => null));
  if (!parsed.success) throw new DecisionError(UNREADABLE);
  return parsed.data;
}

/** The decision, or the candidates when several decisions are about the plan. */
export async function fetchDecision(
  caller: DecisionCaller,
  link: DecisionLink,
): Promise<{ decision: Decision } | { candidates: Candidate[] }> {
  const path =
    "decisionId" in link
      ? `/decisions/${link.decisionId}?${new URLSearchParams({ runId: link.runId })}`
      : `/decisions?${new URLSearchParams({ subjectType: "Plan", subjectId: link.planId, runId: link.runId })}`;
  const response = await get(caller, path);
  if (response.status === 409) {
    const parsed = ambiguousSchema.safeParse(await response.json().catch(() => null));
    if (!parsed.success) throw new DecisionError(UNREADABLE);
    return { candidates: parsed.data.error.details.candidates };
  }
  const decision = await read(response, decisionSchema);
  const matches =
    decision.runScope.runId === link.runId &&
    ("decisionId" in link
      ? decision.decisionId === link.decisionId
      : decision.subject.id === link.planId);
  if (!matches) throw new DecisionError(UNREADABLE);
  return { decision };
}

export async function fetchEvidence(
  caller: DecisionCaller,
  runId: string,
  decisionId: string,
  referenceId: string,
): Promise<Evidence> {
  const response = await get(
    caller,
    `/decisions/${decisionId}/evidence/${encodeURIComponent(referenceId)}?${new URLSearchParams({ runId })}`,
  );
  const evidence = await read(response, evidenceSchema);
  if (evidence.decisionId !== decisionId || evidence.referenceId !== referenceId) {
    throw new DecisionError(UNREADABLE);
  }
  return evidence;
}
