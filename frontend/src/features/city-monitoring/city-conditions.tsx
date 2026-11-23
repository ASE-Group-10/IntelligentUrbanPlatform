import { Layer, Source } from "react-map-gl/maplibre";

import { ConditionLegend } from "@/components/map/condition-legend";
import { useTheme } from "@/lib/theme";

import type { SegmentStatus } from "./city-snapshot";
import { conditionOf, DEMO_SEGMENTS, toFeatures } from "./demo-segments";
import { SEGMENT_HIT_LAYER, useCityConditions } from "./use-city-snapshot";

/** Hex versions of the --status-* tokens in index.css; MapLibre can't read CSS variables. */
const STATUS_COLOURS = {
  light: { open: "#2c965d", slow: "#df911a", closed: "#db2c2b" },
  dark: { open: "#4ebe7d", slow: "#f5ae39", closed: "#fc5950" },
} as const;

const STATUS_LABELS: Record<SegmentStatus, string> = {
  open: "Open",
  slow: "Slow",
  closed: "Closed",
};

export function CityConditionsLayer({ accessToken }: { accessToken?: string }) {
  const { resolvedTheme } = useTheme();
  const { snapshot } = useCityConditions(accessToken);
  const colours = STATUS_COLOURS[resolvedTheme];
  const lineColor = [
    "match",
    ["get", "status"],
    "closed",
    colours.closed,
    "slow",
    colours.slow,
    colours.open,
  ] as const;

  if (!snapshot) return null;

  return (
    <Source id="city-segments" type="geojson" data={toFeatures(DEMO_SEGMENTS, snapshot.segments)}>
      <Layer
        id="city-segments"
        type="line"
        filter={["==", ["get", "quality"], "good"]}
        layout={{ "line-cap": "round", "line-join": "round" }}
        paint={{ "line-color": lineColor as never, "line-width": 6, "line-opacity": 0.85 }}
      />
      <Layer
        id="city-segments-uncertain"
        type="line"
        filter={["!=", ["get", "quality"], "good"]}
        layout={{ "line-join": "round" }}
        paint={{
          "line-color": lineColor as never,
          "line-width": 6,
          "line-opacity": 0.85,
          "line-dasharray": [1.5, 1],
        }}
      />
      <Layer
        id={SEGMENT_HIT_LAYER}
        type="line"
        paint={{ "line-color": "#000", "line-width": 18, "line-opacity": 0 }}
      />
    </Source>
  );
}

const time = new Intl.DateTimeFormat("en-IE", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "Europe/Dublin",
});

function formatTime(value: string | null | undefined) {
  return value ? time.format(new Date(value)) : "—";
}

const ERROR_TEXT = {
  signin: "Sign in to see live road conditions.",
  forbidden: "You don’t have access to these road conditions.",
  unavailable: "Conditions unavailable right now. Retrying every few seconds.",
  failed: "Couldn’t read road conditions.",
} as const;

export function CityConditionsPanel({
  accessToken,
  selectedKey,
}: {
  accessToken?: string;
  selectedKey?: string;
}) {
  const { snapshot, errorKind, lastKnown, isPending } = useCityConditions(accessToken);
  const selected = DEMO_SEGMENTS.find((segment) => segment.segmentKey === selectedKey);
  const condition = snapshot && selected && conditionOf(selected.segmentKey, snapshot.segments);

  return (
    <div className="space-y-3 text-xs">
      <ConditionLegend />
      {errorKind ? (
        <p role="alert" className="text-destructive">
          {ERROR_TEXT[errorKind]}
        </p>
      ) : (
        isPending && <p className="text-muted-foreground">Loading road conditions…</p>
      )}
      {snapshot && (
        <p className="text-muted-foreground">
          {lastKnown ? "Last known: " : ""}City State v{snapshot.snapshotVersion} · demo run ·{" "}
          {formatTime(snapshot.generatedAt)}
          {snapshot.staleSources.length > 0 &&
            ` · old data from ${snapshot.staleSources.join(", ")} shown dashed`}
        </p>
      )}
      {selected && condition ? (
        <section aria-label={selected.name} className="space-y-1 rounded-lg border p-3">
          <h2 className="font-medium text-foreground">{selected.name}</h2>
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
            <dt className="text-muted-foreground">Status</dt>
            <dd>{STATUS_LABELS[condition.status]}</dd>
            <dt className="text-muted-foreground">Cause</dt>
            <dd>{condition.cause ?? "—"}</dd>
            <dt className="text-muted-foreground">Valid until</dt>
            <dd>{formatTime(condition.validUntil)}</dd>
            <dt className="text-muted-foreground">Source</dt>
            <dd>{condition.sources?.join(", ") ?? "—"}</dd>
          </dl>
          {condition.quality !== "good" && (
            <p className="text-status-slow">
              Uncertain: last observed {formatTime(condition.observedAt)}.
            </p>
          )}
        </section>
      ) : (
        snapshot && <p className="text-muted-foreground">Click a road to see its details.</p>
      )}
    </div>
  );
}
