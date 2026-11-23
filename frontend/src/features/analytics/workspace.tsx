import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import type { ResourceScope } from "@/features/auth/resource-access";
import { ProviderPicker } from "@/features/auth/resource-access";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

type Indicator = {
  metricId: string;
  value: number | null;
  unit: string;
  reason?: string;
  lastUpdated: string | null;
};
type KPISet = { status: string; generatedAt: string; failure?: string; indicators: Indicator[] };
type Fact = {
  deliveryId: string;
  status: string;
  progressStatus: string;
  occurredAt: string;
  sourceEventId: string;
};
type Manifest = {
  runId: string;
  scenarioId: string;
  status: string;
  availableRanges: { start: string; end: string }[];
};
type Frame = {
  sessionId: string;
  runId: string;
  cursor: number;
  status: string;
  simulationTime: string;
  state: Record<string, unknown>;
  recordedKpis: Indicator[];
  decisionIds: string[];
  interruption?: string;
};
const inputClass = "rounded-lg border border-input bg-background px-3 py-2 text-sm";

export function AnalyticsWorkspace({ providerId, request }: ResourceScope) {
  const [runId, setRunId] = useState("");
  const [start, setStart] = useState(() => new Date().toISOString().slice(0, 10) + "T00:00");
  const [end, setEnd] = useState(
    () => new Date(Date.now() + 86400000).toISOString().slice(0, 10) + "T00:00",
  );
  const [result, setResult] = useState<KPISet>();
  const [facts, setFacts] = useState<Fact[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [activeQuery, setActiveQuery] = useState<object>();
  const [runs, setRuns] = useState<Manifest[]>([]);
  const [selected, setSelected] = useState("");
  const [frame, setFrame] = useState<Frame>();
  const [seek, setSeek] = useState("0");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  const sessionId = frame?.sessionId;
  const api = useCallback(
    async (path: string, body?: object, signal?: AbortSignal, key?: string) => {
      const response = await request(path, {
        method: body ? "POST" : "GET",
        signal,
        headers: {
          "Content-Type": "application/json",
          "X-Acting-Provider": providerId,
          ...(key ? { "Idempotency-Key": key } : {}),
        },
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!response.ok) {
        if (response.status === 403) {
          setResult(undefined);
          setFacts([]);
          setFrame(undefined);
          setRuns([]);
        }
        if (response.status === 409 && sessionId && path.startsWith("/replay-sessions/")) {
          const latest = await request(`/replay-sessions/${sessionId}`, {
            method: "GET",
            signal,
            headers: { "X-Acting-Provider": providerId },
          });
          if (latest.ok) setFrame(await latest.json());
        }
        const data = await response.json().catch(() => null);
        throw new Error(data?.error?.message ?? `Request failed (${response.status}).`);
      }
      return response.json();
    },
    [providerId, request, sessionId],
  );

  async function perform(action: () => Promise<void>) {
    setPending(true);
    setError("");
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed.");
    } finally {
      setPending(false);
    }
  }

  async function load(event: FormEvent) {
    event.preventDefault();
    await perform(async () => {
      const query = {
        metricDefinitionVersion: "unconfigured-v1",
        scope: {
          providerId,
          scope: runId ? "simulation" : "live",
          runId: runId || null,
          period: {
            start: new Date(start + ":00Z").toISOString(),
            end: new Date(end + ":00Z").toISOString(),
          },
        },
      };
      setResult(undefined);
      setFacts([]);
      setNextCursor(null);
      const summary = await api("/analytics/kpis/query", query);
      setResult(summary);
      const page = await api("/analytics/kpis/delivery-time/breakdown?limit=20", query);
      setFacts(page.records);
      setNextCursor(page.nextCursor);
      setActiveQuery(query);
    });
  }

  async function control(command: string, targetCursor?: number) {
    if (!frame) return;
    await perform(async () =>
      setFrame(
        await api(
          `/replay-sessions/${frame.sessionId}/controls`,
          {
            command,
            expectedCursor: frame.cursor,
            ...(targetCursor !== undefined ? { targetCursor } : {}),
          },
          undefined,
          crypto.randomUUID(),
        ),
      ),
    );
  }

  useEffect(() => {
    if (frame?.status !== "playing") return;
    const abort = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const next = await api(
          `/replay-sessions/${frame.sessionId}/advance`,
          { expectedCursor: frame.cursor },
          abort.signal,
        );
        if (!abort.signal.aborted) setFrame(next);
      } catch (e) {
        if (!abort.signal.aborted) setError(e instanceof Error ? e.message : "Replay read failed.");
      }
    }, 1000);
    return () => {
      clearTimeout(timer);
      abort.abort();
    };
  }, [frame, api]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="KPIs and replay"
        description="Provider-scoped observations and saved simulation recordings."
      />
      <ProviderPicker />
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
      <Card>
        <CardHeader>
          <CardTitle>Source observations</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Metric formulas and populations are awaiting agreement. Values remain unavailable;
            source records are shown separately. Times below are UTC.
          </p>
          <form onSubmit={load} className="flex flex-wrap items-end gap-3">
            <label className="grid gap-1 text-sm">
              From (UTC)
              <input
                className={inputClass}
                type="datetime-local"
                required
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </label>
            <label className="grid gap-1 text-sm">
              To (UTC)
              <input
                className={inputClass}
                type="datetime-local"
                required
                value={end}
                onChange={(e) => setEnd(e.target.value)}
              />
            </label>
            <label className="grid gap-1 text-sm">
              Simulation run ID (blank for live)
              <input
                className={inputClass}
                value={runId}
                onChange={(e) => setRunId(e.target.value)}
              />
            </label>
            <Button type="submit" disabled={pending}>
              Refresh observations
            </Button>
          </form>
          {result && (
            <>
              <p className="text-sm">
                Status: {result.status} · Refreshed {result.generatedAt}
                {result.failure ? ` · ${result.failure}` : ""}
              </p>
              <div className="grid gap-3 sm:grid-cols-2">
                {result.indicators.map((i) => (
                  <div key={i.metricId} className="rounded-lg border p-3">
                    <p className="font-medium">{i.metricId}</p>
                    <p>{i.value === null ? "Unavailable" : `${i.value} ${i.unit}`}</p>
                    <p className="text-sm text-muted-foreground">{i.reason}</p>
                  </div>
                ))}
              </div>
              <p className="text-sm">Source facts below are not calculated KPI contributions.</p>
              <ul className="space-y-2">
                {facts.map((f) => (
                  <li className="rounded-lg border p-3 text-sm" key={f.sourceEventId}>
                    <p>{f.deliveryId}</p>
                    <p>
                      {f.status} · {f.progressStatus} · {f.occurredAt}
                    </p>
                  </li>
                ))}
              </ul>
              {!facts.length && <p>No authorised, bound observations in this period.</p>}
              {nextCursor && (
                <Button
                  variant="outline"
                  disabled={pending}
                  onClick={() =>
                    perform(async () => {
                      const page = await api(
                        `/analytics/kpis/delivery-time/breakdown?limit=20&cursor=${encodeURIComponent(nextCursor)}`,
                        activeQuery,
                      );
                      setFacts(page.records);
                      setNextCursor(page.nextCursor);
                    })
                  }
                >
                  Next page
                </Button>
              )}
            </>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Saved simulation replay</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Playback uses saved outcomes only. A synthetic or partial recording does not prove a
            complete simulation; inspect its declared scenario and coverage.
          </p>
          <div className="flex flex-wrap gap-3">
            <Button
              variant="outline"
              disabled={pending}
              onClick={() =>
                perform(async () => {
                  const data = await api(`/simulation-runs?providerId=${providerId}&limit=100`);
                  setRuns(data.items);
                  setSelected(data.items[0]?.runId ?? "");
                })
              }
            >
              Load saved runs
            </Button>
            <select
              aria-label="Saved run"
              className={inputClass}
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
            >
              <option value="">Select a run</option>
              {runs.map((r) => (
                <option key={r.runId} value={r.runId}>
                  {r.scenarioId} ({r.status})
                </option>
              ))}
            </select>
            <Button
              disabled={pending || !selected}
              onClick={() =>
                perform(async () => {
                  const run = runs.find((r) => r.runId === selected);
                  const range = run?.availableRanges[0];
                  if (!range) throw new Error("This run has no replayable range.");
                  const value = await api(
                    "/replay-sessions",
                    { runId: selected, timeRange: range, speed: 1 },
                    undefined,
                    crypto.randomUUID(),
                  );
                  if (!value.sessionId)
                    throw new Error(value.reason ?? "Cannot replay this range.");
                  setFrame(value);
                })
              }
            >
              Open replay
            </Button>
          </div>
          {frame && (
            <>
              <p role="status">
                {frame.status} · Cursor {frame.cursor} · {frame.simulationTime}
              </p>
              {frame.interruption && <p role="alert">{frame.interruption}</p>}
              <div className="flex flex-wrap gap-2">
                {(["play", "pause", "resume", "stop"] as const).map((c) => (
                  <Button
                    key={c}
                    variant="outline"
                    disabled={
                      pending ||
                      !(c === "play"
                        ? frame.status === "ready"
                        : c === "pause"
                          ? frame.status === "playing"
                          : c === "resume"
                            ? frame.status === "paused"
                            : ["ready", "playing", "paused"].includes(frame.status))
                    }
                    onClick={() => control(c)}
                  >
                    {c}
                  </Button>
                ))}
                <input
                  aria-label="Seek cursor"
                  className={inputClass}
                  type="number"
                  min="0"
                  step="1"
                  value={seek}
                  onChange={(e) => setSeek(e.target.value)}
                />
                <Button
                  variant="outline"
                  disabled={pending || ["completed", "stopped"].includes(frame.status)}
                  onClick={() => control("seek", Number(seek))}
                >
                  Seek
                </Button>
              </div>
              <p className="text-sm text-muted-foreground">
                Recorded decisions: {frame.decisionIds.length}. Historical explanations are not
                available in this recording view.
              </p>
              <dl className="grid gap-2 text-sm">
                {Object.entries(frame.state)
                  .filter(([, value]) => typeof value !== "object")
                  .map(([key, value]) => (
                    <div key={key} className="flex gap-3">
                      <dt className="font-medium">{key.replace(/([A-Z])/g, " $1")}</dt>
                      <dd>{String(value)}</dd>
                    </div>
                  ))}
              </dl>
              {frame.state.deliveries && typeof frame.state.deliveries === "object" ? (
                <ul className="space-y-2">
                  {Object.entries(frame.state.deliveries).map(([id, value]) => {
                    const delivery = value as {
                      status?: string;
                      progress?: { status?: string; updatedAt?: string };
                    };
                    return (
                      <li key={id} className="rounded-lg border p-3 text-sm">
                        <p>{id}</p>
                        <p>
                          {delivery.status} · {delivery.progress?.status} ·{" "}
                          {delivery.progress?.updatedAt}
                        </p>
                      </li>
                    );
                  })}
                </ul>
              ) : null}
              {frame.recordedKpis.length > 0 && (
                <ul>
                  {frame.recordedKpis.map((i) => (
                    <li key={i.metricId}>
                      {i.metricId}: {i.value === null ? "Unavailable" : `${i.value} ${i.unit}`}
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
