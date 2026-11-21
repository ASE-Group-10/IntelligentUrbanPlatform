import { z } from "zod";

import { config } from "@/lib/config";

/** C06 `getCitySnapshot` (contracts/components/c06-city-monitoring/openapi.yaml). */
const segmentConditionSchema = z.object({
  segmentKey: z.string().regex(/^seg:[1-9][0-9]*$/),
  status: z.enum(["open", "slow", "closed"]),
  speedKmh: z.number().nullable().optional(),
  delaySeconds: z.number().nullable().optional(),
  cause: z
    .enum(["traffic", "roadworks", "event", "weather", "incident", "manual"])
    .nullable()
    .optional(),
  validUntil: z.string().nullable().optional(),
  observedAt: z.string(),
  quality: z.enum(["good", "degraded", "unknown"]),
  sources: z.array(z.string()).min(1),
  conflict: z.boolean().nullable().optional(),
});

const citySnapshotSchema = z.object({
  snapshotVersion: z.number().int().min(1),
  generatedAt: z.string(),
  segments: z.array(segmentConditionSchema),
  staleSources: z.array(z.string()),
  overallQuality: z.enum(["good", "degraded", "unknown"]),
});

export type SegmentCondition = z.infer<typeof segmentConditionSchema>;
export type CitySnapshot = z.infer<typeof citySnapshotSchema>;
export type SegmentStatus = SegmentCondition["status"];
export type Quality = SegmentCondition["quality"];

/** `signin`: no or expired session; `unavailable`: C06 has no data or is down. */
export type CitySnapshotErrorKind = "signin" | "unavailable" | "failed";

export class CitySnapshotError extends Error {
  constructor(readonly kind: CitySnapshotErrorKind) {
    super(`City State request failed: ${kind}`);
  }
}

export interface CitySnapshotRequest {
  segmentKeys: readonly string[];
  /** Simulation run to read; leave out for live City State. */
  runId?: string;
  accessToken?: string;
  signal?: AbortSignal;
}

export async function fetchCitySnapshot({
  segmentKeys,
  runId,
  accessToken,
  signal,
}: CitySnapshotRequest): Promise<CitySnapshot> {
  const params = new URLSearchParams({ segmentKeys: segmentKeys.join(",") });
  if (runId) params.set("runId", runId);
  const headers = new Headers();
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);

  const base = config.apiBaseUrl.replace(/\/$/, "");
  const response = await fetch(`${base}/v1/city-state/snapshot?${params}`, {
    headers,
    signal,
  });
  if (response.status === 401) throw new CitySnapshotError("signin");
  if (response.status >= 500) throw new CitySnapshotError("unavailable");
  if (!response.ok) throw new CitySnapshotError("failed");

  const parsed = citySnapshotSchema.safeParse(await response.json().catch(() => null));
  if (!parsed.success) throw new CitySnapshotError("failed");
  return parsed.data;
}
