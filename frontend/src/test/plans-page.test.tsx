import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import proposal from "../../../contracts/components/c05-planning/examples/plan-proposal.json";
import scenario from "../../../fixtures/dublin-scenario.json";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const auth = vi.hoisted(() => ({
  onAuthStateChange: vi.fn(),
  getSession: vi.fn(),
  refreshSession: vi.fn(),
}));
vi.mock("@/features/auth/supabase", () => ({ supabase: { auth } }));

const session = {
  user: { id: "20000000-0000-4000-8000-000000000001" },
  access_token: "test-token",
};
const provider = { providerId: "10000000-0000-4000-8000-000000000001", providerName: "Dublin" };
const runScope = proposal.runScope;
const trace = scenario.correlationId;
const planPath = `/api/v1/plans/${proposal.planId}`;
const link = `/plans?${new URLSearchParams({ planId: proposal.planId, ...runScope, correlationId: trace })}`;
const confirmed = { ...proposal, version: proposal.version + 1, status: "confirmed" };

async function openPlans(
  planApi: (url: string, init: RequestInit) => Response | Promise<Response>,
  url = link,
  role = "operator",
  assignments = () => Response.json([]),
) {
  auth.onAuthStateChange.mockImplementation((callback) => {
    callback("INITIAL_SESSION", session);
    return { data: { subscription: { unsubscribe: vi.fn() } } };
  });
  auth.getSession.mockResolvedValue({ data: { session } });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init: RequestInit) => {
      if (path.endsWith("/identity/me"))
        return Response.json({
          principalId: session.user.id,
          principalType: "user",
          displayName: "Dublin operator",
          availableProviders: [provider],
          memberships: [{ ...provider, roles: [role] }],
          platformRoles: [],
        });
      if (path.includes("/vehicle-assignments")) return assignments();
      return planApi(path, init);
    }),
  );
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createRouter({
    routeTree,
    context: { queryClient },
    history: createMemoryHistory({ initialEntries: [url] }),
  });
  render(
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ThemeProvider>,
  );
  await act(() => router.load());
  return router;
}

afterEach(() => vi.unstubAllGlobals());

it("keeps an unmapped segment in route order and lets the operator confirm", async () => {
  const route = {
    ...proposal.routes[0],
    segmentKeys: ["seg:4471023", "seg:999999999", "seg:4471024"],
  };
  await openPlans((_url, init) =>
    Response.json({ ...(init.method === "POST" ? confirmed : proposal), routes: [route] }),
  );

  const routeText = "Route: Eden Quay → seg:999999999 → Abbey Street Lower";
  expect(await screen.findByText(routeText)).toBeVisible();
  expect(screen.getByText(`Plan ID ${proposal.planId}`)).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "Confirm plan" }));
  expect(await screen.findByText(`Plan confirmed · Version ${confirmed.version}`)).toBeVisible();
  expect(screen.getByText(routeText)).toBeVisible();
});

it("shows the saved ID and revision when reopening the same plan", async () => {
  const revised = {
    ...confirmed,
    version: confirmed.version + 1,
    routes: [{ ...confirmed.routes[0], segmentKeys: ["seg:4471024"] }],
  };
  await openPlans(() => Response.json(revised));
  expect(await screen.findByText(`Plan confirmed · Version ${revised.version}`)).toBeVisible();
  expect(screen.getByText(`Plan ID ${proposal.planId}`)).toBeVisible();
  expect(screen.getByText("Route: Abbey Street Lower")).toBeVisible();
  expect(await screen.findByText(/no vehicle assignment for this plan version yet/i)).toBeVisible();
});

it("keeps the allocation hint in the confirmed run until the matching assignment is visible", async () => {
  const target = {
    planId: proposal.planId,
    planVersion: confirmed.version,
    deliveryId: proposal.requestIds[0],
    assignmentRef: "dublin-van",
    resourceName: "Dublin van",
    status: "allocated",
  };
  let rows = [{ ...target, planId: "d0713709-dedc-4b08-b5ba-e3a61a7ea8b2" }];
  let saved = proposal;
  await openPlans(
    (_url, init) => {
      if (init.method === "POST") saved = confirmed;
      return Response.json(saved);
    },
    link,
    "operator",
    () => Response.json(rows),
  );
  await userEvent.click(await screen.findByRole("button", { name: "Confirm plan" }));
  const hint = /no vehicle assignment for this plan version yet/i;
  expect(await screen.findByText(hint)).toBeVisible();
  const scope = screen.getByRole("combobox", { name: "Plan and assignment scope" });
  await userEvent.selectOptions(scope, "");
  expect(screen.queryByText(hint)).not.toBeInTheDocument();
  await userEvent.selectOptions(scope, runScope.runId);
  expect(await screen.findByText(hint)).toBeVisible();
  rows = [target];
  await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
  expect(await screen.findByText("Vehicle allocated")).toBeVisible();
  expect(screen.queryByText(hint)).not.toBeInTheDocument();
});

it("retries a 401 confirmation once with the same provider, command, trace and key", async () => {
  const sends: RequestInit[] = [];
  auth.refreshSession.mockResolvedValue({
    data: { session: { ...session, access_token: "refreshed-token" } },
  });
  await openPlans((_url, init) => {
    if (init.method !== "POST") return Response.json(proposal);
    sends.push(init);
    return sends.length === 1 ? new Response(null, { status: 401 }) : Response.json(confirmed);
  });
  await userEvent.click(await screen.findByRole("button", { name: "Confirm plan" }));
  expect(await screen.findByText(`Plan confirmed · Version ${confirmed.version}`)).toBeVisible();
  expect(sends).toHaveLength(2);
  const [first, retry] = sends as [RequestInit, RequestInit];
  const initial = new Headers(first.headers);
  const retried = new Headers(retry.headers);
  expect(initial.get("Authorization")).toBe("Bearer test-token");
  expect(retried.get("Authorization")).toBe("Bearer refreshed-token");
  expect(initial.get("Idempotency-Key")).toBeTruthy();
  for (const name of ["Idempotency-Key", "X-Acting-Provider", "X-Correlation-ID"]) {
    expect(retried.get(name)).toBe(initial.get(name));
  }
  expect(initial.get("X-Acting-Provider")).toBe(provider.providerId);
  expect(initial.get("X-Correlation-ID")).toBe(trace);
  expect(retry.body).toBe(first.body);
});

it("opens the newly linked proposal in the same run after viewing live assignments", async () => {
  const next = { ...proposal, planId: "d0713709-dedc-4b08-b5ba-e3a61a7ea8b2" };
  const router = await openPlans((url) =>
    Response.json(url.includes(next.planId) ? next : proposal),
  );
  await screen.findByRole("button", { name: "Confirm plan" });
  const scope = screen.getByRole("combobox", { name: "Plan and assignment scope" });
  await userEvent.selectOptions(scope, "");
  expect(screen.queryByRole("heading", { name: "Delivery plan" })).not.toBeInTheDocument();

  await act(() =>
    router.navigate({
      to: "/plans",
      search: { ...runScope, planId: next.planId, correlationId: trace },
    }),
  );
  expect(await screen.findByRole("button", { name: "Confirm plan" })).toBeVisible();
  expect(screen.getByRole("combobox", { name: "Plan and assignment scope" })).toHaveValue(
    runScope.runId,
  );
});

it("opens the linked run and retries an unacknowledged confirmation with the original command", async () => {
  const sends: RequestInit[] = [];
  let finish!: (response: Response) => void;
  await openPlans((url, init) => {
    const headers = new Headers(init.headers);
    expect(headers.get("Authorization")).toBe("Bearer test-token");
    expect(headers.get("X-Acting-Provider")).toBe(provider.providerId);
    expect(headers.get("X-Correlation-ID")).toBe(trace);
    if (url === `${planPath}?${new URLSearchParams(runScope)}`) return Response.json(proposal);
    expect(url).toBe(`${planPath}/confirmation`);
    sends.push(init);
    if (sends.length === 1) throw new Error("Response lost");
    return new Promise((resolve) => {
      finish = resolve;
    });
  });
  const button = await screen.findByRole("button", { name: "Confirm plan" });
  expect(screen.getByText(/Route: Eden Quay/)).toBeVisible();
  expect(screen.getByRole("combobox", { name: "Plan and assignment scope" })).toHaveValue(
    runScope.runId,
  );
  expect(
    button.compareDocumentPosition(screen.getByRole("heading", { name: "Vehicle assignments" })) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  await userEvent.click(button);
  expect(await screen.findByRole("alert")).toHaveTextContent(/retry/i);
  await userEvent.dblClick(button);
  expect(screen.getByRole("button", { name: "Confirming…" })).toBeDisabled();
  expect(sends).toHaveLength(2);
  const [first, retry] = sends as [RequestInit, RequestInit];
  expect(first.method).toBe("POST");
  expect(JSON.parse(String(first.body))).toEqual({ expectedVersion: proposal.version, runScope });
  expect(retry.body).toBe(first.body);
  expect(new Headers(first.headers).get("Idempotency-Key")).toBeTruthy();
  expect(new Headers(retry.headers).get("Idempotency-Key")).toBe(
    new Headers(first.headers).get("Idempotency-Key"),
  );
  await act(() => finish(Response.json(confirmed)));
  expect(await screen.findByText(`Plan confirmed · Version ${confirmed.version}`)).toBeVisible();
  expect(screen.queryByRole("button", { name: "Confirm plan" })).not.toBeInTheDocument();
});

it("reloads the saved version after a conflict before allowing any further action", async () => {
  let reads = 0;
  await openPlans((_url, init) => {
    if (init.method === "POST") return new Response(null, { status: 409 });
    return Response.json(++reads === 1 ? proposal : confirmed);
  });
  await userEvent.click(await screen.findByRole("button", { name: "Confirm plan" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/conflict/i);
  expect(screen.queryByRole("button", { name: "Confirm plan" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Reload plan" }));
  expect(await screen.findByText(/Plan confirmed/)).toBeVisible();
});

it("explains a rejected plan read without offering confirmation", async () => {
  await openPlans(() => new Response(null, { status: 400 }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/original delivery link/i);
  expect(screen.queryByRole("button", { name: "Confirm plan" })).not.toBeInTheDocument();
});

it("reloads the proposal after a rejected confirmation without resending the command", async () => {
  const requests: string[] = [];
  await openPlans((_url, init) => {
    const method = init.method ?? "GET";
    requests.push(method);
    return method === "POST" ? new Response(null, { status: 400 }) : Response.json(proposal);
  });
  await userEvent.click(await screen.findByRole("button", { name: "Confirm plan" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/invalid.*reload the plan/i);
  expect(screen.queryByRole("button", { name: "Confirm plan" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Reload plan" }));
  expect(await screen.findByRole("button", { name: "Confirm plan" })).toBeEnabled();
  expect(requests).toEqual(["GET", "POST", "GET"]);
});

it.each([
  ["driver", link, /operator access/i],
  ["operator", `/plans?planId=${proposal.planId}`, /proposal link is incomplete/i],
])(
  "keeps assignments available without issuing a plan request for %s on %s",
  async (role, url, message) => {
    const planApi = vi.fn();
    await openPlans(planApi, url, role);
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByRole("heading", { name: "Vehicle assignments" })).toBeVisible();
    expect(planApi).not.toHaveBeenCalled();
  },
);
