import { z } from "zod";

import type { SessionRequest } from "@/features/auth/identity";

// The C04 handoff carries the delivery's original run and correlation ID.
const searchField = z.string().optional().catch("");
export const plansSearch = z.object({
  planId: searchField,
  runId: searchField,
  scope: searchField,
  correlationId: searchField,
});
export const proposalLink = z.object({
  planId: z.uuidv4().toLowerCase(),
  runId: z.uuidv4().toLowerCase(),
  scope: z.literal("simulation").default("simulation"),
  correlationId: z.uuidv4().toLowerCase(),
});
export type ProposalLink = z.infer<typeof proposalLink>;

const planSchema = z.object({
  planId: z.uuid(),
  version: z.number().int().positive(),
  status: z.enum(["awaiting_confirmation", "confirmed"]),
  requestIds: z.array(z.uuid()).min(1),
  runScope: z.object({ scope: z.literal("simulation"), runId: z.uuid() }),
  routes: z.array(z.object({ segmentKeys: z.array(z.string()) })).min(1),
});
export type Plan = z.infer<typeof planSchema>;

export class PlanError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function requestPlan(
  request: SessionRequest,
  providerId: string,
  link: ProposalLink,
  signal: AbortSignal,
  expectedVersion?: number,
): Promise<Plan> {
  const runScope = { scope: link.scope, runId: link.runId };
  const path = `/plans/${link.planId}`;
  const headers: Record<string, string> = {
    "X-Acting-Provider": providerId,
    "X-Correlation-ID": link.correlationId,
  };
  let response: Response;
  if (expectedVersion === undefined) {
    response = await request(`${path}?${new URLSearchParams(runScope)}`, { headers, signal });
  } else {
    headers["Content-Type"] = "application/json";
    // Stable across double clicks, lost responses, page reloads and token refresh.
    headers["Idempotency-Key"] = `confirm:${link.planId}:v${expectedVersion}`;
    response = await request(`${path}/confirmation`, {
      method: "POST",
      headers,
      signal,
      body: JSON.stringify({ expectedVersion, runScope }),
    });
  }
  if (!response.ok) {
    const messages: Record<number, string> = {
      401: "Sign in to view this plan.",
      403: "You don’t have access to this plan.",
      404: "This plan could not be found in the selected run.",
      409: "Confirmation conflicted with the saved plan or delivery. Reload the plan before trying again.",
    };
    throw new PlanError(
      response.status,
      messages[response.status] ?? "The planning service is unavailable.",
    );
  }
  const result = planSchema.parse(await response.json());
  if (result.planId !== link.planId || result.runScope.runId !== link.runId) {
    throw new Error("Unexpected plan or run.");
  }
  if (
    expectedVersion !== undefined &&
    (result.status !== "confirmed" || result.version <= expectedVersion)
  ) {
    throw new Error("Confirmation could not be verified.");
  }
  return result;
}
