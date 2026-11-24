import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import proposal from "../../../contracts/components/c05-planning/examples/plan-proposal.json";
import scenario from "../../../fixtures/dublin-scenario.json";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const auth = vi.hoisted(() => ({ onAuthStateChange: vi.fn(), getSession: vi.fn() }));
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
      if (path.includes("/vehicle-assignments")) return Response.json([]);
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
}

afterEach(() => vi.unstubAllGlobals());

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
