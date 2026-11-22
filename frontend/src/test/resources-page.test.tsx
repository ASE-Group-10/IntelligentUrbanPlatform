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
const session = {
  user: { id: "20000000-0000-4000-8000-000000000001" },
  access_token: "synthetic-token",
};
const providers = [
  { providerId: providerA, providerName: "Provider A" },
  { providerId: providerB, providerName: "Provider B" },
];
const defaultProfile = {
  principalId: session.user.id,
  principalType: "user",
  displayName: "Test manager",
  availableProviders: providers,
  memberships: providers.map((provider) => ({ ...provider, roles: ["manager"] })),
  platformRoles: [] as string[],
};
let profile: typeof defaultProfile;
let currentSession: typeof session | null;
type AuthEvent = (event: string, value: typeof currentSession) => void;
let notify: AuthEvent;
beforeEach(() => {
  vi.resetAllMocks();
  currentSession = session;
  profile = structuredClone(defaultProfile);
  auth.onAuthStateChange.mockImplementation((callback: AuthEvent) => {
    notify = callback;
    callback("INITIAL_SESSION", currentSession);
    return { data: { subscription: { unsubscribe: vi.fn() } } };
  });
  auth.getSession.mockImplementation(async () => ({ data: { session: currentSession } }));
  auth.signOut.mockImplementation(async () => {
    currentSession = null;
    notify("SIGNED_OUT", null);
    return { error: null };
  });
});

const resource = (id: string, name: string) => ({
  id,
  external_identifier: id,
  name,
  resource_type: "vehicle",
  profile_status: "active",
  observed_availability: "available",
  agent_status: "active",
  planning_eligible: true,
  eligibility_reasons: [],
  version: 1,
  address: "x",
  latitude: 0,
  longitude: 0,
  details: {},
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

async function renderResources(selectFirst = true) {
  const queryClient = new QueryClient();
  const router = createRouter({
    routeTree,
    context: { queryClient },
    history: createMemoryHistory({ initialEntries: ["/resources"] }),
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
  if (selectFirst)
    await userEvent.selectOptions(await screen.findByLabelText("Acting provider"), providerA);
}

type Handler = (
  identity: string,
  signal?: AbortSignal | null,
  init?: RequestInit,
) => Promise<Response>;

function mockFetch(listHandler: Handler) {
  vi.stubGlobal(
    "fetch",
    vi.fn((path: string, init: RequestInit = {}) => {
      const headers = new Headers(init.headers);
      expect(headers.get("Authorization")).toBe(`Bearer ${currentSession?.access_token}`);
      if (path.endsWith("/identity/me")) {
        expect(headers.has("X-Acting-Provider")).toBe(false);
        return Promise.resolve(json(profile));
      }
      const provider = headers.get("X-Acting-Provider");
      if (!provider) throw new Error("Missing acting provider");
      if (path.includes("/development/config"))
        return Promise.resolve(
          json({ locations: [{ address: "Dublin Depot", latitude: 53.3, longitude: -6.2 }] }),
        );
      return listHandler(provider, init.signal, { ...init, ...{ method: init.method ?? "GET" } });
    }),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("Resources page provider switching", () => {
  it("registers a resource on the initial identity under StrictMode", async () => {
    let abortedAtSubmit: boolean | undefined;
    mockFetch((_identity, signal, init) => {
      if (init?.method === "POST") {
        abortedAtSubmit = signal?.aborted;
        return Promise.resolve(json(resource("a1", "A van"), 201));
      }
      return Promise.resolve(json({ items: [] }));
    });
    const user = userEvent.setup();
    await renderResources();
    await screen.findByText("0 saved");
    await user.type(screen.getByPlaceholderText("VAN-001"), "VAN-A");
    await user.type(screen.getByPlaceholderText("Dublin electric van"), "A van");
    await user.click(screen.getByRole("button", { name: "Register resource" }));

    expect(abortedAtSubmit).toBe(false);
    expect(await screen.findByText("A van registered successfully")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Register resource" })).toBeEnabled();
  });

  it("ignores a slow response from the previous identity", async () => {
    let resolveA: (response: Response) => void = () => {};
    mockFetch((identity) =>
      identity === providerA
        ? new Promise<Response>((resolve) => {
            resolveA = resolve;
          })
        : Promise.resolve(json({ items: [resource("b1", "Provider B van")] })),
    );
    const user = userEvent.setup();
    await renderResources();

    await user.selectOptions(await screen.findByLabelText("Acting provider"), providerB);
    expect(await screen.findByText("Provider B van")).toBeInTheDocument();

    resolveA(json({ items: [resource("a1", "Provider A van")] }));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText("Provider A van")).not.toBeInTheDocument();
    expect(screen.getByText("Provider B van")).toBeInTheDocument();
  });

  it("clears the previous list and shows the error when the new identity fails", async () => {
    mockFetch((identity) =>
      Promise.resolve(
        identity === providerA
          ? json({ items: [resource("a1", "Provider A van")] })
          : json({ error: { message: "Provider B failed" } }, 500),
      ),
    );
    const user = userEvent.setup();
    await renderResources();
    expect(await screen.findByText("Provider A van")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Acting provider"), providerB);

    expect(await screen.findByText("Provider B failed")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Provider A van")).not.toBeInTheDocument());
    expect(screen.getByText("0 saved")).toBeInTheDocument();
  });

  it("leaves the new provider alone when a registration from the previous one completes", async () => {
    let resolvePost: (response: Response) => void = () => {};
    mockFetch((identity, _signal, init) => {
      if (init?.method === "POST") {
        return new Promise<Response>((resolve) => {
          resolvePost = resolve;
        });
      }
      return Promise.resolve(
        json({ items: identity === providerA ? [] : [resource("b1", "Provider B van")] }),
      );
    });
    const user = userEvent.setup();
    await renderResources();
    await screen.findByText("0 saved");

    await user.type(screen.getByPlaceholderText("VAN-001"), "VAN-A");
    await user.type(screen.getByPlaceholderText("Dublin electric van"), "A van");
    await user.click(screen.getByRole("button", { name: "Register resource" }));

    await user.selectOptions(screen.getByLabelText("Acting provider"), providerB);
    expect(await screen.findByText("Provider B van")).toBeInTheDocument();
    await user.type(screen.getByPlaceholderText("VAN-001"), "VAN-B");

    resolvePost(json(resource("a1", "A van"), 201));
    await new Promise((r) => setTimeout(r, 20));

    expect(screen.queryByText(/registered successfully/)).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText("VAN-001")).toHaveValue("VAN-B");
    expect(screen.getByText("Provider B van")).toBeInTheDocument();
    expect(screen.getByText("1 saved")).toBeInTheDocument();
  });

  it("does not show a failed registration from the previous provider", async () => {
    let rejectPost: (response: Response) => void = () => {};
    mockFetch((_identity, _signal, init) =>
      init?.method === "POST"
        ? new Promise<Response>((resolve) => {
            rejectPost = resolve;
          })
        : Promise.resolve(json({ items: [] })),
    );
    const user = userEvent.setup();
    await renderResources();
    await screen.findByText("0 saved");

    await user.type(screen.getByPlaceholderText("VAN-001"), "VAN-A");
    await user.type(screen.getByPlaceholderText("Dublin electric van"), "A van");
    await user.click(screen.getByRole("button", { name: "Register resource" }));
    await user.selectOptions(screen.getByLabelText("Acting provider"), providerB);
    await screen.findByText("0 saved");

    rejectPost(json({ error: { message: "Duplicate identifier" } }, 409));
    await new Promise((r) => setTimeout(r, 20));

    expect(screen.queryByText("Duplicate identifier")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Register resource" })).toBeEnabled();
  });

  it("clears drafts and messages when the identity changes", async () => {
    mockFetch(() => Promise.resolve(json({ items: [] })));
    const user = userEvent.setup();
    await renderResources();
    await user.type(await screen.findByPlaceholderText("VAN-001"), "VAN-A");

    await user.selectOptions(screen.getByLabelText("Acting provider"), providerB);

    await waitFor(() => expect(screen.getByPlaceholderText("VAN-001")).toHaveValue(""));
  });
});

async function submitVan() {
  const user = userEvent.setup();
  await user.type(await screen.findByPlaceholderText("VAN-001"), "SYN-001");
  await user.type(screen.getByPlaceholderText("Dublin electric van"), "Synthetic van");
  await user.click(screen.getByRole("button", { name: "Register resource" }));
}

it("signs in, limits registration by role, and clears the workspace on sign-out", async () => {
  currentSession = null;
  profile.memberships = providers.map((provider) => ({
    ...provider,
    roles: [provider.providerId === providerB ? "operator" : "manager"],
  }));
  auth.signInWithPassword.mockImplementation(async () => {
    currentSession = session;
    notify("SIGNED_IN", session);
    return { error: null };
  });
  mockFetch(() => Promise.resolve(json({ items: [resource("x", "Scoped van")] })));
  await renderResources(false);
  expect(fetch).not.toHaveBeenCalled();
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("Email"), "manager@example.test");
  await user.type(screen.getByLabelText("Password"), "synthetic-password");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  expect(auth.signInWithPassword).toHaveBeenCalledWith({
    email: "manager@example.test",
    password: "synthetic-password",
  });
  const chooser = await screen.findByLabelText("Acting provider");
  expect(screen.queryByText("Scoped van")).not.toBeInTheDocument();
  await user.selectOptions(chooser, providerB);
  expect(await screen.findByText("Scoped van")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Register resource" })).not.toBeInTheDocument();
  await user.selectOptions(chooser, providerA);
  await user.type(await screen.findByPlaceholderText("VAN-001"), "draft");
  await user.click(screen.getByRole("button", { name: "Sign out" }));
  expect(await screen.findByLabelText("Email")).toBeVisible();
  expect(screen.queryByText("Scoped van")).not.toBeInTheDocument();
  expect(screen.queryByDisplayValue("draft")).not.toBeInTheDocument();
});

it("requires an admin with no memberships to select even a single available provider", async () => {
  profile.memberships = [];
  profile.platformRoles = ["admin"];
  profile.availableProviders = providers.slice(1);
  mockFetch((provider, _, init) => {
    expect(provider).toBe(providerB);
    return Promise.resolve(
      init?.method === "POST" ? json(resource("x", "Synthetic van"), 201) : json({ items: [] }),
    );
  });
  await renderResources(false);
  const chooser = await screen.findByLabelText("Acting provider");
  expect(screen.queryByRole("button", { name: "Register resource" })).not.toBeInTheDocument();
  await userEvent.selectOptions(chooser, providerB);
  await submitVan();
  expect(await screen.findByText("Synthetic van registered successfully")).toBeVisible();
});

it("refreshes an expired token and retries a registration with the same idempotency key", async () => {
  profile.availableProviders = providers.slice(0, 1);
  const attempts: Headers[] = [];
  auth.refreshSession.mockImplementation(async () => {
    currentSession = { ...session, access_token: "refreshed-token" };
    notify("TOKEN_REFRESHED", currentSession);
    return { data: { session: currentSession } };
  });
  mockFetch((provider, _, init) => {
    expect(provider).toBe(providerA);
    if (init?.method !== "POST") return Promise.resolve(json({ items: [] }));
    attempts.push(new Headers(init.headers));
    return Promise.resolve(
      attempts.length === 1 ? json({}, 401) : json(resource("x", "Synthetic van"), 201),
    );
  });
  await renderResources(false);
  await submitVan();
  expect(await screen.findByText("Synthetic van registered successfully")).toBeVisible();
  expect(attempts.map((h) => h.get("Authorization"))).toEqual([
    "Bearer synthetic-token",
    "Bearer refreshed-token",
  ]);
  expect(attempts[0]?.get("Idempotency-Key")).toBeTruthy();
  expect(attempts[1]?.get("Idempotency-Key")).toBe(attempts[0]?.get("Idempotency-Key"));
});

it("returns to sign-in when an expired registration session cannot be refreshed", async () => {
  auth.refreshSession.mockRejectedValue(new Error("Refresh failed"));
  mockFetch((_, __, init) =>
    Promise.resolve(
      init?.method === "POST" ? json({}, 401) : json({ items: [resource("x", "Scoped van")] }),
    ),
  );
  await renderResources();
  await submitVan();
  expect(await screen.findByLabelText("Email")).toBeVisible();
  expect(screen.queryByText("Scoped van")).not.toBeInTheDocument();
  expect(auth.signOut).toHaveBeenCalledWith({ scope: "local" });
});

it("clears restricted data and reports a server denial without revealing its body", async () => {
  mockFetch((_, __, init) =>
    Promise.resolve(
      init?.method === "POST"
        ? json({ error: { message: "Restricted detail" } }, 403)
        : json({ items: [resource("x", "Scoped van")] }),
    ),
  );
  await renderResources();
  expect(await screen.findByText("Scoped van")).toBeVisible();
  await submitVan();
  expect(await screen.findByRole("alert")).toHaveTextContent("Access denied for this provider.");
  expect(screen.queryByText("Restricted detail")).not.toBeInTheDocument();
  expect(screen.queryByText("Scoped van")).not.toBeInTheDocument();
  expect(screen.queryByText(/registered successfully/)).not.toBeInTheDocument();
});
