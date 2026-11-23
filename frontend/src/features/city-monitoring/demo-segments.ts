import { z } from "zod";

import scenario from "../../../../fixtures/dublin-scenario.json";

import type { Quality, SegmentCondition, SegmentStatus } from "./city-snapshot";

/**
 * C06 holds segment keys without geometry, so the map takes the demo road
 * shapes from the shared scenario (fixtures/dublin-scenario.json, #43).
 */
const scenarioSchema = z.object({
  runId: z.uuid(),
  segments: z.array(
    z.object({
      segmentKey: z.string(),
      name: z.string(),
      geometry: z.object({
        type: z.literal("LineString"),
        coordinates: z.array(z.tuple([z.number(), z.number()])).min(2),
      }),
    }),
  ),
});

const demo = scenarioSchema.parse(scenario);

/** Simulation run the demo closure is fed into. */
export const DEMO_RUN_ID = demo.runId;
export const DEMO_SEGMENTS = demo.segments;
export type DemoSegment = (typeof DEMO_SEGMENTS)[number];

export interface SegmentProperties {
  segmentKey: string;
  status: SegmentStatus;
  quality: Quality;
}

export interface SegmentFeatures {
  type: "FeatureCollection";
  features: {
    type: "Feature";
    properties: SegmentProperties;
    geometry: DemoSegment["geometry"];
  }[];
}

/** Absent from the snapshot means open with good quality (C06 contract). */
export function conditionOf(
  segmentKey: string,
  conditions: readonly SegmentCondition[] = [],
): Pick<SegmentCondition, "status" | "quality"> & Partial<SegmentCondition> {
  return (
    conditions.find((condition) => condition.segmentKey === segmentKey) ?? {
      status: "open",
      quality: "good",
    }
  );
}

export function toFeatures(
  segments: readonly DemoSegment[],
  conditions?: readonly SegmentCondition[],
): SegmentFeatures {
  return {
    type: "FeatureCollection",
    features: segments.map(({ segmentKey, geometry }) => {
      const { status, quality } = conditionOf(segmentKey, conditions);
      return { type: "Feature", properties: { segmentKey, status, quality }, geometry };
    }),
  };
}
