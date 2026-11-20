import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { queryClient } from "@/lib/query-client";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

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

async function renderResources() {
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
  );
  await router.load();
}

type Handler = (identity: string, signal?: AbortSignal | null) => Promise<Response>;

function mockFetch(listHandler: Handler) {
  vi.stubGlobal(
    "fetch",
    vi.fn((path: string, init: RequestInit = {}) => {
      const identity = String((init.headers as Record<string, string>).Authorization).replace("Bearer ", "");
      if (path.includes("/development/config")) return Promise.resolve(json({ locations: [] }));
      return listHandler(identity, init.signal);
    }),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("Resources page identity switching", () => {
  it("ignores a slow response from the previous identity", async () => {
    let resolveA: (response: Response) => void = () => {};
    mockFetch((identity) =>
      identity === "dev-manager"
        ? new Promise<Response>((resolve) => {
            resolveA = resolve;
          })
        : Promise.resolve(json({ items: [resource("b1", "Provider B van")] })),
    );
    const user = userEvent.setup();
    await renderResources();

    await user.selectOptions(await screen.findByLabelText("Identity"), "dev-other-provider");
    expect(await screen.findByText("Provider B van")).toBeInTheDocument();

    resolveA(json({ items: [resource("a1", "Provider A van")] }));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText("Provider A van")).not.toBeInTheDocument();
    expect(screen.getByText("Provider B van")).toBeInTheDocument();
  });

  it("clears the previous list and shows the error when the new identity fails", async () => {
    mockFetch((identity) =>
      Promise.resolve(
        identity === "dev-manager"
          ? json({ items: [resource("a1", "Provider A van")] })
          : json({ error: { message: "Provider B failed" } }, 500),
      ),
    );
    const user = userEvent.setup();
    await renderResources();
    expect(await screen.findByText("Provider A van")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Identity"), "dev-other-provider");

    expect(await screen.findByText("Provider B failed")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Provider A van")).not.toBeInTheDocument());
    expect(screen.getByText("0 saved")).toBeInTheDocument();
  });
});
