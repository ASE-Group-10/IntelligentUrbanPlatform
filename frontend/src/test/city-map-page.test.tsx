import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";

import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

// MapLibre needs WebGL, which jsdom lacks: keep the page, drop the canvas.
vi.mock("@/components/map/city-map", () => ({
  CityMap: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));
vi.mock("react-map-gl/maplibre", () => ({ Source: () => null, Layer: () => null }));

const auth = vi.hoisted(() => ({
  onAuthStateChange: vi.fn(),
  signInWithPassword: vi.fn(),
  getSession: vi.fn(),
  refreshSession: vi.fn(),
  signOut: vi.fn(),
}));
vi.mock("@/features/auth/supabase", () => ({ supabase: { auth } }));

const session = {
  user: { id: "20000000-0000-4000-8000-000000000001" },
  access_token: "synthetic-token",
};
const profile = {
  principalId: session.user.id,
  principalType: "user",
  displayName: "Test operator",
  availableProviders: [],
  memberships: [],
  platformRoles: [],
};
const snapshot = {
  snapshotVersion: 42,
  generatedAt: "2026-11-20T08:14:35Z",
  segments: [
    {
      segmentKey: "seg:4471023",
      status: "closed",
      cause: "roadworks",
      validUntil: "2026-11-20T18:00:00Z",
      observedAt: "2026-11-20T08:14:30Z",
      quality: "good",
      sources: ["sim-traffic"],
    },
  ],
  staleSources: [],
  overallQuality: "good",
};

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Stand-in API: C02 profile, and C06 only answers a request with a bearer token. */
function stubApi() {
  const snapshotAuth: (string | null)[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init: RequestInit = {}) => {
      const authorization = new Headers(init.headers).get("Authorization");
      if (path.endsWith("/identity/me")) return json(200, profile);
      if (path.includes("/city-state/snapshot")) {
        snapshotAuth.push(authorization);
        return authorization
          ? json(200, snapshot)
          : json(401, { error: { code: "UNAUTHENTICATED", message: "x" } });
      }
      return json(404, {});
    }),
  );
  return snapshotAuth;
}

async function renderMap(signedIn: boolean, path = "/") {
  auth.onAuthStateChange.mockImplementation((callback) => {
    callback("INITIAL_SESSION", signedIn ? session : null);
    return { data: { subscription: { unsubscribe: vi.fn() } } };
  });
  auth.getSession.mockResolvedValue({ data: { session: signedIn ? session : null } });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createRouter({
    routeTree,
    context: { queryClient },
    history: createMemoryHistory({ initialEntries: [path] }),
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

describe("City map page", () => {
  it("loads road conditions with the signed-in user's token", async () => {
    const snapshotAuth = stubApi();

    await renderMap(true);

    expect(await screen.findByText(/City State v42/)).toBeInTheDocument();
    expect(snapshotAuth.at(-1)).toBe("Bearer synthetic-token");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("asks to sign in when there is no session", async () => {
    const snapshotAuth = stubApi();

    await renderMap(false);

    expect(await screen.findByRole("alert")).toHaveTextContent(/sign in/i);
    expect(snapshotAuth.every((value) => value === null)).toBe(true);
  });

  it("opens with the road from ?segment= selected", async () => {
    stubApi();

    await renderMap(true, "/?segment=seg:4471023");

    const details = await screen.findByRole("region", { name: "Eden Quay" });
    expect(details).toHaveTextContent("Closed");
    expect(details).toHaveTextContent("roadworks");
  });

  it("ignores a malformed ?segment=", async () => {
    stubApi();

    await renderMap(true, "/?segment=not-a-segment");

    expect(await screen.findByText(/Click a road to see its details/)).toBeInTheDocument();
  });
});
