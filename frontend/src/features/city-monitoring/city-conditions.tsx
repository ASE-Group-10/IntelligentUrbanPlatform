import { Layer, Source } from "react-map-gl/maplibre";

import { ConditionLegend } from "@/components/map/condition-legend";
import { useTheme } from "@/lib/theme";

import { CitySnapshotError, type SegmentStatus } from "./city-snapshot";
import { conditionOf, DEMO_SEGMENTS, toFeatures } from "./demo-segments";
import { SEGMENT_HIT_LAYER, useCitySnapshot } from "./use-city-snapshot";

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
  const { data } = useCitySnapshot(accessToken);
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

  return (
    <Source id="city-segments" type="geojson" data={toFeatures(DEMO_SEGMENTS, data?.segments)}>
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
  const { data, error, isPending } = useCitySnapshot(accessToken);
  const kind = error instanceof CitySnapshotError ? error.kind : error ? "failed" : null;
  const selected = DEMO_SEGMENTS.find((segment) => segment.segmentKey === selectedKey);
  const condition = selected && conditionOf(selected.segmentKey, data?.segments);

  return (
    <div className="space-y-3 text-xs">
      <ConditionLegend />
      {kind ? (
        <p role="alert" className="text-destructive">
          {ERROR_TEXT[kind]}
        </p>
      ) : isPending ? (
        <p className="text-muted-foreground">Loading road conditions…</p>
      ) : (
        data && (
          <p className="text-muted-foreground">
            City State v{data.snapshotVersion} · demo run · {formatTime(data.generatedAt)}
            {data.staleSources.length > 0 &&
              ` · old data from ${data.staleSources.join(", ")} shown dashed`}
          </p>
        )
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
        <p className="text-muted-foreground">Click a road to see its details.</p>
      )}
    </div>
  );
}
