import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";

import { CityConditionsPanel } from "./city-conditions";
import {
  CitySnapshotError,
  fetchCitySnapshot,
  type CitySnapshot,
  type SegmentCondition,
} from "./city-snapshot";
import { DEMO_RUN_ID, DEMO_SEGMENTS, toFeatures } from "./demo-segments";

const CLOSED: SegmentCondition = {
  segmentKey: "seg:4471023",
  status: "closed",
  speedKmh: null,
  delaySeconds: null,
  cause: "roadworks",
  validUntil: "2026-11-20T18:00:00Z",
  observedAt: "2026-11-20T08:14:30Z",
  quality: "good",
  sources: ["sim-traffic"],
};

const SNAPSHOT: CitySnapshot = {
  snapshotVersion: 42,
  generatedAt: "2026-11-20T08:14:35Z",
  segments: [CLOSED],
  staleSources: [],
  overallQuality: "good",
};

function respond(status: number, body: unknown) {
  return vi.fn(async () => new Response(JSON.stringify(body), { status }));
}

function wrap(children: ReactNode, client = newClient()) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function newClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

/** One response per poll, in order; the last one repeats. */
function respondInTurn(...replies: [number, unknown][]) {
  let call = 0;
  return vi.fn(async () => {
    const [status, body] = replies[Math.min(call++, replies.length - 1)] ?? [500, null];
    return new Response(JSON.stringify(body), { status });
  });
}

const UNAVAILABLE = { error: { code: "Unavailable", message: "down" } };
const UNAUTHENTICATED = { error: { code: "UNAUTHENTICATED", message: "x" } };

afterEach(() => vi.unstubAllGlobals());

describe("demo segments", () => {
  it("has geometry for the scenario's closure and detour", () => {
    expect(DEMO_SEGMENTS.map((segment) => segment.segmentKey)).toEqual([
      "seg:4471023",
      "seg:4471024",
    ]);
  });

  it("colours recorded segments and treats the others as open and good", () => {
    const features = toFeatures(DEMO_SEGMENTS, [{ ...CLOSED, quality: "degraded" }]);

    expect(features.features.map((feature) => feature.properties)).toEqual([
      { segmentKey: "seg:4471023", status: "closed", quality: "degraded" },
      { segmentKey: "seg:4471024", status: "open", quality: "good" },
    ]);
  });
});

describe("fetchCitySnapshot", () => {
  it("asks for the run's segments with the access token", async () => {
    const fetch = respond(200, SNAPSHOT);
    vi.stubGlobal("fetch", fetch);

    const snapshot = await fetchCitySnapshot({
      segmentKeys: ["seg:4471023", "seg:4471024"],
      runId: DEMO_RUN_ID,
      accessToken: "token",
    });

    expect(snapshot).toEqual(SNAPSHOT);
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    const params = new URL(url, "http://localhost").searchParams;
    expect(params.get("segmentKeys")).toBe("seg:4471023,seg:4471024");
    expect(params.get("runId")).toBe(DEMO_RUN_ID);
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer token");
  });

  it.each([
    [401, "signin"],
    [403, "forbidden"],
    [503, "unavailable"],
    [500, "unavailable"],
    [400, "failed"],
  ] as const)("maps HTTP %i to %s", async (status, kind) => {
    vi.stubGlobal("fetch", respond(status, { error: { code: "x", message: "x" } }));

    const result = fetchCitySnapshot({ segmentKeys: ["seg:1"] });

    await expect(result).rejects.toBeInstanceOf(CitySnapshotError);
    await expect(result).rejects.toMatchObject({ kind });
  });

  it("rejects a response that doesn't match the contract", async () => {
    vi.stubGlobal("fetch", respond(200, { ...SNAPSHOT, segments: [{ segmentKey: "x" }] }));

    await expect(fetchCitySnapshot({ segmentKeys: ["seg:1"] })).rejects.toMatchObject({
      kind: "failed",
    });
  });
});

describe("CityConditionsPanel", () => {
  it("shows the snapshot version and the selected segment's details", async () => {
    vi.stubGlobal("fetch", respond(200, SNAPSHOT));

    render(wrap(<CityConditionsPanel selectedKey="seg:4471023" />));

    expect(await screen.findByText(/City State v42/)).toBeInTheDocument();
    const details = screen.getByRole("region", { name: "Eden Quay" });
    expect(details).toHaveTextContent("Closed");
    expect(details).toHaveTextContent("roadworks");
    expect(details).toHaveTextContent("sim-traffic");
    expect(details).toHaveTextContent(/Valid until/);
  });

  it("marks a degraded segment as uncertain", async () => {
    vi.stubGlobal(
      "fetch",
      respond(200, {
        ...SNAPSHOT,
        segments: [{ ...CLOSED, quality: "degraded" }],
        overallQuality: "degraded",
        staleSources: ["sim-traffic"],
      }),
    );

    render(wrap(<CityConditionsPanel selectedKey="seg:4471023" />));

    const details = await screen.findByRole("region", { name: "Eden Quay" });
    await waitFor(() => expect(details).toHaveTextContent(/Uncertain: last observed/));
    expect(screen.getByText(/old data from sim-traffic/)).toBeInTheDocument();
  });

  it("shows an unselected open segment as open", async () => {
    vi.stubGlobal("fetch", respond(200, SNAPSHOT));

    render(wrap(<CityConditionsPanel selectedKey="seg:4471024" />));

    const details = await screen.findByRole("region", { name: "Abbey Street Lower" });
    await waitFor(() => expect(details).toHaveTextContent("Open"));
  });

  it("says conditions are unavailable instead of showing an empty map", async () => {
    vi.stubGlobal("fetch", respond(503, { error: { code: "Unavailable", message: "down" } }));

    render(wrap(<CityConditionsPanel />));

    expect(await screen.findByRole("alert")).toHaveTextContent(/conditions unavailable/i);
  });

  it("asks to sign in when there is no session", async () => {
    vi.stubGlobal("fetch", respond(401, { error: { code: "UNAUTHENTICATED", message: "x" } }));

    render(wrap(<CityConditionsPanel />));

    expect(await screen.findByRole("alert")).toHaveTextContent(/sign in/i);
  });

  it("shows no road state before C06 has returned a snapshot", async () => {
    vi.stubGlobal("fetch", respond(503, UNAVAILABLE));

    render(wrap(<CityConditionsPanel selectedKey="seg:4471023" />));

    expect(await screen.findByRole("alert")).toHaveTextContent(/conditions unavailable/i);
    expect(screen.queryByRole("region", { name: "Eden Quay" })).not.toBeInTheDocument();
    expect(screen.queryByText("Status")).not.toBeInTheDocument();
  });

  it("drops the closure when the next poll needs sign-in", async () => {
    vi.stubGlobal("fetch", respondInTurn([200, SNAPSHOT], [401, UNAUTHENTICATED]));
    const client = newClient();
    render(wrap(<CityConditionsPanel selectedKey="seg:4471023" />, client));
    expect(await screen.findByRole("region", { name: "Eden Quay" })).toHaveTextContent("roadworks");

    await act(() => client.refetchQueries());

    expect(await screen.findByRole("alert")).toHaveTextContent(/sign in/i);
    expect(screen.queryByRole("region", { name: "Eden Quay" })).not.toBeInTheDocument();
    expect(screen.queryByText(/roadworks|City State v/)).not.toBeInTheDocument();
  });

  it("keeps the closure as last known when the next poll is unavailable", async () => {
    vi.stubGlobal("fetch", respondInTurn([200, SNAPSHOT], [503, UNAVAILABLE]));
    const client = newClient();
    render(wrap(<CityConditionsPanel selectedKey="seg:4471023" />, client));
    expect(await screen.findByText(/^City State v42/)).toBeInTheDocument();

    await act(() => client.refetchQueries());

    expect(await screen.findByRole("alert")).toHaveTextContent(/conditions unavailable/i);
    expect(screen.getByText(/Last known: City State v42/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Eden Quay" })).toHaveTextContent("Closed");
  });

  it("says the session can't read the run on a 403", async () => {
    vi.stubGlobal("fetch", respond(403, { error: { code: "Forbidden", message: "x" } }));

    render(wrap(<CityConditionsPanel selectedKey="seg:4471023" />));

    expect(await screen.findByRole("alert")).toHaveTextContent(/don’t have access/i);
    expect(screen.queryByRole("region", { name: "Eden Quay" })).not.toBeInTheDocument();
  });
});
