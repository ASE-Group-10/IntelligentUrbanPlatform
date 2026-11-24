import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const auth = vi.hoisted(() => ({
  onAuthStateChange: vi.fn(),
  signInWithPassword: vi.fn(),
  getSession: vi.fn(),
  refreshSession: vi.fn(),
  signOut: vi.fn(),
}));
vi.mock("@/features/auth/supabase", () => ({ supabase: { auth } }));

const providerA = "10000000-0000-4000-8000-000000000001";
const providerB = "10000000-0000-4000-8000-000000000002";
const deliveryId = "7c6b5a49-3827-4165-a0f9-e8d7c6b5a493";
const planId = "9e2c4b6a-1d3f-4e5a-8b7c-6d5e4f3a2b1c";
const originalTrace = "f73f2a1b-d771-46e2-80dc-1a6568976e8e";

const session = {
  user: { id: "20000000-0000-4000-8000-000000000001" },
  access_token: "synthetic-token",
};
const providers = [
  { providerId: providerA, providerName: "Dublin Fleet A" },
  { providerId: providerB, providerName: "Dublin Fleet B" },
];
const defaultProfile = {
  principalId: session.user.id,
  principalType: "user",
  displayName: "Test operator",
  availableProviders: providers,
  memberships: [
    { ...providers[0], roles: ["operator"] },
    { ...providers[1], roles: ["driver"] },
  ],
  platformRoles: [] as string[],
};

let profile: typeof defaultProfile;
let currentSession: typeof session | null;

beforeEach(() => {
  vi.resetAllMocks();
  currentSession = session;
  profile = structuredClone(defaultProfile);
  auth.onAuthStateChange.mockImplementation(
    (callback: (event: string, s: typeof session | null) => void) => {
      callback("INITIAL_SESSION", currentSession);
      return { data: { subscription: { unsubscribe: vi.fn() } } };
    },
  );
  auth.getSession.mockImplementation(async () => ({ data: { session: currentSession } }));
  auth.signOut.mockResolvedValue({ error: null });
});

afterEach(() => vi.unstubAllGlobals());

const sampleDelivery = (overrides: Record<string, unknown> = {}) => ({
  deliveryId,
  version: 1,
  status: "pending",
  pickup: { address: "Dublin Port", latitude: 53.3478, longitude: -6.2297 },
  destination: { address: "Trinity College Dublin", latitude: 53.3438, longitude: -6.2546 },
  cargo: { description: "Office supplies", weightKg: 4.5, parcelCount: 2 },
  timeWindow: { start: "2026-11-20T09:00:00Z", end: "2026-11-20T12:00:00Z" },
  constraints: { vehicleKinds: ["van", "cargo_bike"], fragile: false },
  plannedAt: null,
  progress: { status: "pending", updatedAt: "2026-11-20T08:00:00Z", note: null },
  createdAt: "2026-11-20T08:00:00Z",
  updatedAt: "2026-11-20T08:00:00Z",
  ...overrides,
});

const json = (body: unknown, status = 200, extraHeaders: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...extraHeaders },
  });

async function renderDeliveries(selectProvider = providerA) {
  const queryClient = new QueryClient();
  const router = createRouter({
    routeTree,
    context: { queryClient },
    history: createMemoryHistory({ initialEntries: ["/deliveries"] }),
  });
  render(
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ThemeProvider>,
    { reactStrictMode: true },
  );
  await act(() => router.load());
  if (selectProvider) {
    await userEvent.selectOptions(await screen.findByLabelText("Acting provider"), selectProvider);
  }
  return router;
}

describe("Deliveries page (C01 → C04)", () => {
  it.each([false, true])(
    "opens only a real proposal with its original submission trace (stub=%s)",
    async (stub) => {
      const calls: {
        url: string;
        method: string;
        headers: Headers;
        body?: Record<string, unknown>;
      }[] = [];
      let items: ReturnType<typeof sampleDelivery>[] = [];

      vi.stubGlobal(
        "fetch",
        vi.fn((url: string, init: RequestInit = {}) => {
          const headers = new Headers(init.headers);
          if (url.endsWith("/identity/me")) return Promise.resolve(json(profile));
          const method = init.method ?? "GET";
          const body = init.body ? JSON.parse(String(init.body)) : undefined;
          calls.push({ url, method, headers, body });
          if (url.includes(`/plans/${planId}`)) {
            const runId = new URL(url, "http://localhost").searchParams.get("runId");
            return Promise.resolve(
              json({
                planId,
                version: 1,
                status: "awaiting_confirmation",
                requestIds: [deliveryId],
                runScope: { scope: "simulation", runId },
                routes: [{ segmentKeys: ["seg:4471023"] }],
              }),
            );
          }
          if (url.includes("vehicle-assignments")) return Promise.resolve(json([]));
          if (method === "POST") {
            const saved = sampleDelivery();
            items = [saved];
            return Promise.resolve(
              json(saved, 201, {
                "X-Correlation-ID": originalTrace,
                "X-Plan-Id": planId,
                "X-Plan-Version": "1",
                ...(stub ? { "X-Plan-Stub": "true" } : {}),
              }),
            );
          }
          return Promise.resolve(json({ items, nextCursor: null }));
        }),
      );

      const user = userEvent.setup();
      await renderDeliveries();
      await screen.findByText("0 saved");

      await user.click(screen.getByRole("button", { name: "Submit delivery" }));

      expect(await screen.findByText(new RegExp(`Delivery ${deliveryId} submitted`))).toBeVisible();
      expect(screen.getByText(new RegExp(planId))).toBeVisible();
      expect(await screen.findByText("Dublin Port → Trinity College Dublin")).toBeVisible();

      const postCall = calls.find((c) => c.method === "POST");
      if (!postCall) throw new Error("Expected a delivery submission");
      expect(postCall?.url).toContain("/api/v1/deliveries?runId=");
      expect(postCall?.headers.get("X-Acting-Provider")).toBe(providerA);
      expect(postCall?.headers.get("X-Correlation-ID")).toBeTruthy();
      expect(postCall?.body?.idempotencyKey).toBeTruthy();
      expect(postCall?.body?.cargo).toEqual({
        description: "Office supplies",
        weightKg: 4.5,
        parcelCount: 2,
      });
      if (stub) {
        expect(screen.queryByRole("link", { name: "Review plan" })).toBeNull();
        return;
      }
      const run = new URL(postCall.url, "http://localhost").searchParams.get("runId");
      await user.click(screen.getByRole("link", { name: "Review plan" }));
      expect(await screen.findByRole("button", { name: "Confirm plan" })).toBeVisible();
      const read = calls.find((c) => c.url.includes(`/plans/${planId}`));
      if (!read) throw new Error("Expected a plan read");
      const query = new URL(read.url, "http://localhost").searchParams;
      expect(query.get("scope")).toBe("simulation");
      expect(query.get("runId")).toBe(run);
      expect(read?.headers.get("X-Correlation-ID")).toBe(originalTrace);
      expect(read?.headers.get("X-Acting-Provider")).toBe(providerA);
    },
  );

  it("keeps a chosen window until the Dublin preset is explicitly reset to the current UTC window", async () => {
    const clock = vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-11-27T23:45:00Z"));
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          json(url.endsWith("/identity/me") ? profile : { items: [], nextCursor: null }),
        ),
      ),
    );
    try {
      const user = userEvent.setup();
      await renderDeliveries();
      const start = await screen.findByLabelText("Window start (UTC)");
      const end = screen.getByLabelText("Window end (UTC)");
      expect(start).toHaveValue("2026-11-28T00:15:00.000Z");
      expect(end).toHaveValue("2026-11-28T03:15:00.000Z");
      await user.clear(start);
      await user.type(start, "2026-12-01T08:00:00Z");
      clock.mockReturnValue(Date.parse("2026-11-30T08:00:00Z"));
      await user.click(screen.getByRole("button", { name: "Refresh deliveries" }));
      expect(start).toHaveValue("2026-12-01T08:00:00Z");
      await user.click(screen.getByRole("button", { name: "Dublin preset" }));
      expect(start).toHaveValue("2026-11-30T08:30:00.000Z");
      expect(end).toHaveValue("2026-11-30T11:30:00.000Z");
    } finally {
      clock.mockRestore();
    }
  });

  it.each(["response lost", "planning unavailable"])(
    "retries the same delivery when %s",
    async (reason) => {
      const attempts: { trace: string | null; key: string }[] = [];
      vi.stubGlobal(
        "fetch",
        vi.fn((url: string, init: RequestInit = {}) => {
          if (url.endsWith("/identity/me")) return Promise.resolve(json(profile));
          if (init.method !== "POST") return Promise.resolve(json({ items: [], nextCursor: null }));
          attempts.push({
            trace: new Headers(init.headers).get("X-Correlation-ID"),
            key: JSON.parse(String(init.body)).idempotencyKey,
          });
          if (attempts.length === 1) {
            return reason === "response lost"
              ? Promise.reject(new Error("Lost response"))
              : Promise.resolve(json(sampleDelivery(), 201));
          }
          return Promise.resolve(
            json(sampleDelivery(), 200, { "X-Plan-Id": planId, "X-Plan-Version": "1" }),
          );
        }),
      );
      const user = userEvent.setup();
      await renderDeliveries();
      const submit = await screen.findByRole("button", { name: "Submit delivery" });
      await user.click(submit);
      if (reason === "response lost") {
        expect(await screen.findByRole("alert")).toHaveTextContent("Lost response");
        await user.click(submit);
      } else {
        expect(
          await screen.findByText(
            "The delivery is saved. Use Retry planning to request its proposal again.",
          ),
        ).toBeVisible();
        await user.click(screen.getByRole("button", { name: "Retry planning" }));
      }
      await screen.findByText(new RegExp(`Delivery ${deliveryId} submitted`));
      expect(await screen.findByRole("link", { name: "Review plan" })).toBeVisible();
      expect(attempts[0]?.trace).toBeTruthy();
      expect(attempts[0]?.key).toBeTruthy();
      expect(attempts[1]).toEqual(attempts[0]);
    },
  );

  it("shows inline field validation errors and does not call POST when invalid", async () => {
    const postSpy = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init: RequestInit = {}) => {
        if (url.endsWith("/identity/me")) return Promise.resolve(json(profile));
        if (init.method === "POST") {
          postSpy();
          return Promise.resolve(json({}, 201));
        }
        return Promise.resolve(json({ items: [], nextCursor: null }));
      }),
    );

    const user = userEvent.setup();
    await renderDeliveries();
    await screen.findByText("0 saved");

    const weightInput = screen.getByLabelText("Weight (kg)");
    await user.clear(weightInput);
    await user.type(weightInput, "0");
    await user.click(screen.getByRole("button", { name: "Submit delivery" }));

    expect(await screen.findByText("Weight must be greater than 0 kg.")).toBeVisible();
    expect(postSpy).not.toHaveBeenCalled();
  });

  it("hides the submit form for a driver role and allows editing a pending delivery for an operator", async () => {
    let saved = sampleDelivery();
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init: RequestInit = {}) => {
        if (url.endsWith("/identity/me")) return Promise.resolve(json(profile));
        if (init.method === "POST" && url.includes(`/deliveries/${deliveryId}`)) {
          const body = JSON.parse(String(init.body));
          expect(body.expectedVersion).toBe(1);
          saved = sampleDelivery({
            version: 2,
            cargo: { ...saved.cargo, description: body.cargo.description },
          });
          return Promise.resolve(json(saved, 200));
        }
        return Promise.resolve(json({ items: [saved], nextCursor: null }));
      }),
    );

    const user = userEvent.setup();
    await renderDeliveries(providerB);
    expect(
      await screen.findByText(
        "You don’t have permission to submit delivery requests for this provider.",
      ),
    ).toBeVisible();

    await user.selectOptions(screen.getByLabelText("Acting provider"), providerA);
    await user.click(await screen.findByRole("button", { name: "Edit" }));

    const inputs = screen.getAllByLabelText("Cargo description");
    const editInput = inputs.at(-1);
    if (!editInput) throw new Error("Expected edit cargo input");
    await user.clear(editInput);
    await user.type(editInput, "Urgent medical parcels");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(screen.getByText(/Urgent medical parcels/)).toBeVisible());
  });
});
