import { z } from "zod";

import { SessionError, type SessionRequest } from "@/features/auth/identity";

export { DEMO_RUN_ID } from "@/features/city-monitoring/demo-segments";

/** C07 `listDisruptions` (contracts/components/c07-disruption-detection/openapi.yaml). */
const affectedPlanSchema = z.object({
  planId: z.uuid(),
  planVersion: z.number().int().min(1),
  replanRequested: z.boolean(),
  replanOutcome: z.enum(["NoChange", "ReviewRequired", "PlanSaved"]).nullable().optional(),
  /** Version C05 saved with `PlanSaved`; with `planVersion` it shows the revision. */
  newPlanVersion: z.number().int().min(1).nullable().optional(),
});

const disruptionSchema = z.object({
  disruptionId: z.uuid(),
  version: z.number().int().min(1),
  status: z.enum(["active", "resolved", "unknown"]),
  cause: z.enum([
    "traffic",
    "roadworks",
    "event",
    "weather",
    "incident",
    "manual",
    "resource",
    "operational",
  ]),
  severity: z.enum(["low", "medium", "high"]),
  segmentKeys: z.array(z.string()),
  resourceIds: z.array(z.uuid()),
  affectedPlans: z.array(affectedPlanSchema),
  triggerEventIds: z.array(z.uuid()),
  quality: z.enum(["good", "degraded", "unknown"]),
  createdAt: z.string(),
  updatedAt: z.string(),
});

const disruptionListSchema = z.object({ disruptions: z.array(disruptionSchema) });

export type Disruption = z.infer<typeof disruptionSchema>;
export type AffectedPlan = z.infer<typeof affectedPlanSchema>;

/** `signin`: no or expired session; `forbidden`: may not read this run; `unavailable`: C07 is down. */
export type DisruptionsErrorKind = "signin" | "forbidden" | "unavailable" | "failed";

export class DisruptionsError extends Error {
  constructor(readonly kind: DisruptionsErrorKind) {
    super(`Disruptions request failed: ${kind}`);
  }
}

export async function fetchDisruptions({
  request,
  runId,
  signal,
}: {
  request: SessionRequest;
  runId?: string;
  signal?: AbortSignal;
}): Promise<Disruption[]> {
  const query = runId ? `?${new URLSearchParams({ runId })}` : "";
  let response: Response;
  try {
    response = await request(`/disruptions${query}`, { signal });
  } catch (error) {
    if (signal?.aborted) throw error;
    // `sessionRequest` throws SessionError once the session can't be refreshed;
    // anything else (a dropped connection) means C07 couldn't be reached.
    throw new DisruptionsError(error instanceof SessionError ? "signin" : "unavailable");
  }
  if (response.status === 401) throw new DisruptionsError("signin");
  if (response.status === 403) throw new DisruptionsError("forbidden");
  if (response.status >= 500) throw new DisruptionsError("unavailable");
  if (!response.ok) throw new DisruptionsError("failed");

  const parsed = disruptionListSchema.safeParse(await response.json().catch(() => null));
  if (!parsed.success) throw new DisruptionsError("failed");
  return parsed.data.disruptions;
}
