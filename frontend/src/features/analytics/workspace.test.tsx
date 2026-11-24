import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AnalyticsWorkspace } from "./workspace";

vi.mock("@/features/auth/resource-access", () => ({ ProviderPicker: () => <p>Test provider</p> }));
const provider = "22222222-2222-4222-8222-222222222222";
const response = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

describe("Analytics workspace", () => {
  afterEach(() => vi.useRealTimers());

  it.each([
    { failure: "503", latestCursor: 0 },
    { failure: "network", latestCursor: 0 },
    { failure: "503", latestCursor: 1 },
  ])("recovers from $failure using saved cursor $latestCursor", async ({ failure, latestCursor }) => {
    vi.useFakeTimers();
    const frame = {
      sessionId: "session-1", runId: "run-1", cursor: 0, status: "ready",
      simulationTime: "2026-11-23T10:00:00Z", state: { synthetic: true },
      recordedKpis: [], decisionIds: [],
    };
    let advances = 0;
    const request = vi.fn(async (path: string) => {
      if (path.startsWith("/simulation-runs")) return response({ items: [{
        runId: "run-1", scenarioId: "partial-c04-capture", status: "incomplete",
        availableRanges: [{ start: "2026-11-23T10:00:00Z", end: "2026-11-23T11:00:00Z" }],
      }] });
      if (path === "/replay-sessions") return response(frame, 201);
      if (path.endsWith("/controls")) return response({ ...frame, status: "playing" });
      if (path === "/replay-sessions/session-1")
        return response({ ...frame, status: "playing", cursor: latestCursor });
      if (path.endsWith("/advance")) {
        if (advances++ === 0) {
          if (failure === "network") throw new Error("Connection lost");
          return response({ error: { message: "Temporarily unavailable" } }, 503);
        }
        return response({ ...frame, status: "completed", cursor: latestCursor + 1 });
      }
      throw new Error(`Unexpected request ${path}`);
    });
    render(<AnalyticsWorkspace providerId={provider} canRegister={false} request={request} />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Load saved runs" })); });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Open replay" })); });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /^play$/ })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByRole("status")).toHaveTextContent("interrupted");
    expect(screen.getByRole("alert")).toHaveTextContent(
      failure === "network" ? "Connection lost" : "Temporarily unavailable",
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(advances).toBe(1);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Retry playback" })); });
    expect(request.mock.calls.some(([path]) => path === "/replay-sessions/session-1")).toBe(true);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByRole("status")).toHaveTextContent("completed");
    const ticks = request.mock.calls.filter(([path]) => path.endsWith("/advance")) as unknown as [string, RequestInit][];
    expect(ticks).toHaveLength(2);
    expect(JSON.parse(String(ticks[1]?.[1].body))).toEqual({ expectedCursor: latestCursor });
  });

  it("sends the acting provider and keeps missing metrics unavailable", async () => {
    const request = vi.fn(async (path: string) =>
      path.endsWith("/query")
        ? response({
            status: "partial",
            generatedAt: "2026-11-23T12:00:00Z",
            indicators: [
              {
                metricId: "cost",
                value: null,
                unit: "EUR",
                lastUpdated: null,
                reason: "Rate not agreed",
              },
            ],
          })
        : response({
            records: [
              {
                deliveryId: "delivery-1",
                status: "delivered",
                progressStatus: "delivered",
                occurredAt: "2026-11-23T11:00:00Z",
                sourceEventId: "event-1",
              },
            ],
            nextCursor: null,
          }),
    );
    render(<AnalyticsWorkspace providerId={provider} canRegister={false} request={request} />);
    await userEvent.click(screen.getByRole("button", { name: "Refresh observations" }));
    expect(await screen.findByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText("Rate not agreed")).toBeInTheDocument();
    expect(screen.getByText("delivery-1")).toBeInTheDocument();
    expect(screen.queryByText("0 EUR")).not.toBeInTheDocument();
    const call = request.mock.calls[0] as unknown as [string, RequestInit];
    expect(new Headers(call[1].headers).get("X-Acting-Provider")).toBe(provider);
    expect(JSON.parse(String(call[1].body)).scope.providerId).toBe(provider);
  });

  it("opens an authorised saved run and sends an expected-cursor control", async () => {
    const frame = {
      sessionId: "session-1",
      cursor: 0,
      status: "ready",
      simulationTime: "2026-11-23T10:00:00Z",
      state: { synthetic: true },
      recordedKpis: [],
      decisionIds: [],
    };
    const request = vi.fn(async (path: string, init: RequestInit) => {
      if (path.startsWith("/simulation-runs"))
        return response({
          items: [
            {
              runId: "run-1",
              scenarioId: "partial-c04-capture",
              status: "incomplete",
              availableRanges: [{ start: "2026-11-23T10:00:00Z", end: "2026-11-23T11:00:00Z" }],
            },
          ],
        });
      if (path.endsWith("/controls")) return response({ ...frame, status: "paused" });
      if (path === "/replay-sessions") return response(frame, 201);
      throw new Error(`Unexpected request ${init.method} ${path}`);
    });
    render(<AnalyticsWorkspace providerId={provider} canRegister={false} request={request} />);
    await userEvent.click(screen.getByRole("button", { name: "Load saved runs" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Open replay" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Open replay" }));
    expect(await screen.findByRole("status")).toHaveTextContent("ready");
    fireEvent.change(screen.getByLabelText("Seek cursor"), { target: { value: "0" } });
    await userEvent.click(screen.getByRole("button", { name: "Seek" }));
    expect(await screen.findByRole("status")).toHaveTextContent("paused");
    const control = request.mock.calls.find(([path]) => path.endsWith("/controls"));
    expect(JSON.parse(String(control?.[1].body))).toEqual({
      command: "seek",
      expectedCursor: 0,
      targetCursor: 0,
    });
    expect(new Headers(control?.[1].headers).get("Idempotency-Key")).toBeTruthy();
  });

  it("shows denied access without presenting invented results", async () => {
    const request = vi.fn(async () => response({ error: { message: "Access denied" } }, 403));
    render(<AnalyticsWorkspace providerId={provider} canRegister={false} request={request} />);
    await userEvent.click(screen.getByRole("button", { name: "Refresh observations" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Access denied");
    expect(screen.queryByText("Status: current")).not.toBeInTheDocument();
  });
});
