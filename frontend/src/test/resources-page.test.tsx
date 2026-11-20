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

type Handler = (
  identity: string,
  signal?: AbortSignal | null,
  init?: RequestInit,
) => Promise<Response>;

function mockFetch(listHandler: Handler) {
  vi.stubGlobal(
    "fetch",
    vi.fn((path: string, init: RequestInit = {}) => {
      const identity = String((init.headers as Record<string, string>).Authorization).replace(
        "Bearer ",
        "",
      );
      if (path.includes("/development/config"))
        return Promise.resolve(
          json({ locations: [{ address: "Dublin Depot", latitude: 53.3, longitude: -6.2 }] }),
        );
      return listHandler(identity, init.signal, { ...init, ...{ method: init.method ?? "GET" } });
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

  it("leaves the new provider alone when a registration from the previous one completes", async () => {
    let resolvePost: (response: Response) => void = () => {};
    mockFetch((identity, _signal, init) => {
      if (init?.method === "POST") {
        return new Promise<Response>((resolve) => {
          resolvePost = resolve;
        });
      }
      return Promise.resolve(
        json({ items: identity === "dev-manager" ? [] : [resource("b1", "Provider B van")] }),
      );
    });
    const user = userEvent.setup();
    await renderResources();
    await screen.findByText("0 saved");

    await user.type(screen.getByPlaceholderText("VAN-001"), "VAN-A");
    await user.type(screen.getByPlaceholderText("Dublin electric van"), "A van");
    await user.click(screen.getByRole("button", { name: "Register resource" }));

    await user.selectOptions(screen.getByLabelText("Identity"), "dev-other-provider");
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
    await user.selectOptions(screen.getByLabelText("Identity"), "dev-other-provider");
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

    await user.selectOptions(screen.getByLabelText("Identity"), "dev-other-provider");

    await waitFor(() => expect(screen.getByPlaceholderText("VAN-001")).toHaveValue(""));
  });
});
