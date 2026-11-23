import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { act, render, screen, within } from "@testing-library/react";

import { AuthContext, type Auth } from "@/features/auth/auth-context";
import { SessionError } from "@/features/auth/identity";

import { DisruptionList } from "./disruption-list";
import { DEMO_RUN_ID, DisruptionsError, fetchDisruptions, type Disruption } from "./disruptions";

const DISRUPTION: Disruption = {
  disruptionId: "d4c3b2a1-0f9e-4d8c-b7a6-5e4d3c2b1a0f",
  version: 1,
  status: "active",
  cause: "roadworks",
  severity: "high",
  segmentKeys: ["seg:4471023"],
  resourceIds: [],
  affectedPlans: [
    {
      planId: "9e2c4b6a-1d3f-4e5a-8b7c-6d5e4f3a2b1c",
      planVersion: 3,
      replanRequested: true,
      replanOutcome: "PlanSaved",
    },
  ],
  triggerEventIds: ["5b1e0c1a-8f0e-4a7e-9d6c-1f2a3b4c5d6e"],
  quality: "good",
  createdAt: "2026-11-20T08:14:32Z",
  updatedAt: "2026-11-20T08:14:33Z",
};

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** One response per call, in order; the last one repeats. */
function replies(...answers: [number, unknown][]) {
  let call = 0;
  return vi.fn<(path: string, init: RequestInit) => Promise<Response>>(async () => {
    const [status, body] = answers[Math.min(call++, answers.length - 1)] ?? [500, null];
    return json(status, body);
  });
}

function signedIn(request: Auth["request"]): Auth {
  return {
    client: {} as Auth["client"],
    session: { user: { id: "user-1" }, access_token: "token" } as Auth["session"],
    accessToken: "token",
    request,
    identityStatus: "success",
    retryIdentity: () => undefined,
    providers: [],
    needsSelection: false,
    selectProvider: () => undefined,
    canRegister: false,
    signOut: async () => true,
  };
}

/** The list inside a bare router, since its cards link to the map. */
function renderList(auth: Auth, client = new QueryClient()) {
  const router = createRouter({
    routeTree: createRootRoute({
      component: () => <DisruptionList runId={DEMO_RUN_ID} runLabel="the Dublin simulation" />,
    }),
    history: createMemoryHistory({ initialEntries: ["/disruptions"] }),
  });
  render(
    <QueryClientProvider client={client}>
      <AuthContext value={auth}>
        <RouterProvider router={router} />
      </AuthContext>
    </QueryClientProvider>,
  );
  return client;
}

const UNAVAILABLE = { error: { code: "Unavailable", message: "x" } };
const FORBIDDEN = { error: { code: "Forbidden", message: "x" } };

describe("fetchDisruptions", () => {
  it("asks for the run's disruptions", async () => {
    const request = replies([200, { disruptions: [DISRUPTION] }]);

    expect(await fetchDisruptions({ request, runId: DEMO_RUN_ID })).toEqual([DISRUPTION]);
    expect(request.mock.calls[0]?.[0]).toBe(`/disruptions?runId=${DEMO_RUN_ID}`);
  });

  it.each([
    [401, "signin"],
    [403, "forbidden"],
    [503, "unavailable"],
    [400, "failed"],
  ] as const)("maps HTTP %i to %s", async (status, kind) => {
    const result = fetchDisruptions({ request: replies([status, FORBIDDEN]) });

    await expect(result).rejects.toBeInstanceOf(DisruptionsError);
    await expect(result).rejects.toMatchObject({ kind });
  });

  it("treats an expired session that can't be refreshed as sign-in", async () => {
    const request = vi.fn(async () => {
      throw new SessionError("Session expired. Sign in again.");
    });

    await expect(fetchDisruptions({ request })).rejects.toMatchObject({ kind: "signin" });
  });

  it("treats a dropped connection as unavailable, not as a lost session", async () => {
    const request = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });

    await expect(fetchDisruptions({ request })).rejects.toMatchObject({
      kind: "unavailable",
    });
  });

  it("rejects a response that doesn't match the contract", async () => {
    const request = replies([200, { disruptions: [{ disruptionId: "x" }] }]);

    await expect(fetchDisruptions({ request })).rejects.toMatchObject({ kind: "failed" });
  });
});

describe("DisruptionList", () => {
  it("shows the closure, its severity and the replan it triggered", async () => {
    renderList(signedIn(replies([200, { disruptions: [DISRUPTION] }])));

    const card = await screen.findByRole("region", { name: /Roadworks on/ });
    expect(card).toHaveTextContent("High severity");
    expect(card).toHaveTextContent("Active");
    expect(card).toHaveTextContent("seg:4471023");
    expect(card).toHaveTextContent("Affected plans (1)");
    expect(card).toHaveTextContent("9e2c4b6a");
    expect(card).toHaveTextContent("version 3");
    expect(card).toHaveTextContent("Revised plan saved");
    expect(screen.getByText(/1 disruption in the Dublin simulation/)).toBeInTheDocument();
  });

  it("links each disrupted road to the map with that road selected", async () => {
    renderList(signedIn(replies([200, { disruptions: [DISRUPTION] }])));

    const link = await screen.findByRole("link", { name: /Show Eden Quay on the map/ });
    expect(link).toHaveAttribute("href", "/?segment=seg%3A4471023");
  });

  it("only links mapped roads on a card with mapped and unmapped segments", async () => {
    const mixed = { ...DISRUPTION, segmentKeys: ["seg:999", "seg:4471023"] };
    renderList(signedIn(replies([200, { disruptions: [mixed] }])));

    const card = within(await screen.findByRole("region", { name: /Roadworks on/ }));
    expect(card.getAllByRole("link")).toHaveLength(1);
    expect(card.getByRole("link", { name: "Show Eden Quay on the map" })).toHaveAttribute(
      "href",
      "/?segment=seg%3A4471023",
    );
    expect(card.getByText("seg:999 · Map unavailable")).toBeVisible();
  });

  it("says a replan is still pending when C05 hasn't taken it", async () => {
    const pending = {
      ...DISRUPTION,
      affectedPlans: [{ ...DISRUPTION.affectedPlans[0], replanRequested: false }],
    };
    renderList(signedIn(replies([200, { disruptions: [pending] }])));

    expect(await screen.findByText(/Replan pending/)).toBeInTheDocument();
  });

  it("explains an empty run", async () => {
    renderList(signedIn(replies([200, { disruptions: [] }])));

    expect(await screen.findByText(/No disruptions yet/)).toBeInTheDocument();
  });

  it("asks to sign in when there is no session", async () => {
    renderList({ ...signedIn(undefined), session: null, request: undefined });

    expect(
      await screen.findByRole("heading", { name: "Sign in to Disruptions" }),
    ).toBeInTheDocument();
  });

  it("keeps the list as last known when C07 becomes unavailable", async () => {
    const client = renderList(
      signedIn(replies([200, { disruptions: [DISRUPTION] }], [503, UNAVAILABLE])),
    );
    await screen.findByRole("region", { name: /Roadworks on/ });

    await act(() => client.refetchQueries());

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable/i);
    expect(screen.getByText(/Last known: 1 disruption/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: /Roadworks on/ })).toBeInTheDocument();
  });

  it("keeps the list hidden after a 403 until a request succeeds again", async () => {
    const client = renderList(
      signedIn(
        replies(
          [200, { disruptions: [DISRUPTION] }],
          [403, FORBIDDEN],
          [503, UNAVAILABLE],
          [200, { disruptions: [DISRUPTION] }],
        ),
      ),
    );
    await screen.findByRole("region", { name: /Roadworks on/ });

    await act(() => client.refetchQueries());
    expect(await screen.findByRole("alert")).toHaveTextContent(/don’t have access/);
    await act(() => client.refetchQueries());

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable/i);
    expect(screen.queryByRole("region", { name: /Roadworks on/ })).not.toBeInTheDocument();

    await act(() => client.refetchQueries());
    expect(await screen.findByRole("region", { name: /Roadworks on/ })).toBeInTheDocument();
  });

  it("keeps the list as last known when the connection drops", async () => {
    let call = 0;
    const request = vi.fn(async () => {
      if (call++ === 0) return json(200, { disruptions: [DISRUPTION] });
      throw new TypeError("Failed to fetch");
    });
    const client = renderList(signedIn(request));
    await screen.findByRole("region", { name: /Roadworks on/ });

    await act(() => client.refetchQueries());

    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable/i);
    expect(screen.queryByText(/session has ended/i)).not.toBeInTheDocument();
    expect(screen.getByText(/Last known: 1 disruption/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: /Roadworks on/ })).toBeInTheDocument();
  });
});
