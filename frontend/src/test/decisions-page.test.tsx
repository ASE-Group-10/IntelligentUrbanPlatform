import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import record from "../../../contracts/components/c10-decision-records/examples/record-decision-request.json";
import scenario from "../../../fixtures/dublin-scenario.json";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const auth = vi.hoisted(() => ({
  onAuthStateChange: vi.fn(),
  getSession: vi.fn(),
  refreshSession: vi.fn(),
}));
vi.mock("@/features/auth/supabase", () => ({ supabase: { auth } }));

function first<T>(items: readonly T[]): T {
  const [item] = items;
  if (item === undefined) throw new Error("expected at least one");
  return item;
}

const session = {
  user: { id: "20000000-0000-4000-8000-000000000001" },
  access_token: "test-token",
};
const provider = { providerId: "10000000-0000-4000-8000-000000000001", providerName: "Dublin" };
const runId = scenario.runId;
const passages = record.evidenceSnapshots.flatMap(({ items }) => items);
const passage = first(passages);
// getDecision's response for the contract's recordDecision example, in the Dublin run.
const decision = {
  ...record,
  runScope: { mode: "Simulation", runId },
  recordedAt: "2026-11-20T06:55:11Z",
  evidenceStatus: passages.map(({ passageId }) => ({
    refId: passageId,
    status: "Stored",
  })),
};
const byId = `/decisions?${new URLSearchParams({ decisionId: decision.decisionId, runId })}`;
const byPlan = `/decisions?${new URLSearchParams({ planId: decision.subject.id })}`;

type DecisionApi = (url: string, init: RequestInit) => Response | Promise<Response>;

async function openDecisions(decisionApi: DecisionApi, url = byId) {
  auth.onAuthStateChange.mockImplementation((callback) => {
    callback("INITIAL_SESSION", session);
    return { data: { subscription: { unsubscribe: vi.fn() } } };
  });
  auth.getSession.mockResolvedValue({ data: { session } });
  auth.refreshSession.mockResolvedValue({ data: { session: null }, error: new Error("expired") });
  const api = vi.fn(decisionApi);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init: RequestInit) => {
      if (path.endsWith("/identity/me"))
        return Response.json({
          principalId: session.user.id,
          principalType: "user",
          displayName: "Dublin operator",
          availableProviders: [provider],
          memberships: [{ ...provider, roles: ["operator"] }],
          platformRoles: [],
        });
      return api(path, init);
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
  return { router, api };
}

const failure = (status: number, code: string, details?: object) =>
  Response.json(
    { error: { code, message: "server text", correlation_id: scenario.correlationId, details } },
    { status },
  );

afterEach(() => vi.unstubAllGlobals());

it("shows the outcome, reasons, rejected options and predicted effects", async () => {
  const { api } = await openDecisions(() => Response.json(decision));

  expect(await screen.findByRole("heading", { name: "Plan generated · version 1" })).toBeVisible();
  expect(screen.getByText("Option selected")).toBeVisible();
  expect(screen.getByText(decision.selectedOption.summary)).toBeVisible();
  for (const reason of decision.reasons) expect(screen.getByText(reason.text)).toBeVisible();
  for (const option of decision.consideredAlternatives) {
    expect(screen.getByText(option.summary)).toBeVisible();
    expect(screen.getByText(`Not selected: ${option.rejectedReason}`)).toBeVisible();
  }
  expect(screen.getByText(`${decision.predictedEffects.costEur} EUR`)).toBeVisible();

  const [url, init] = first(api.mock.calls);
  expect(url).toBe(`/api/v1/decisions/${decision.decisionId}?runId=${runId}`);
  expect(new Headers(init.headers).get("X-Acting-Provider")).toBe(provider.providerId);
});

it("reads a passage only when it is opened", async () => {
  const { api } = await openDecisions((url) =>
    url.includes("/evidence/")
      ? Response.json({
          decisionId: decision.decisionId,
          referenceId: passage.passageId,
          kind: "Evidence",
          status: "Stored",
          evidence: passage,
          alternative: null,
        })
      : Response.json(decision),
  );
  const title = `Evidence: ${passage.documentTitle} § ${passage.sectionId}`;
  const button = first(await screen.findAllByRole("button", { name: title }));
  expect(api).toHaveBeenCalledTimes(1);

  await userEvent.click(button);

  expect(await screen.findByText(passage.content)).toBeVisible();
  expect(button).toHaveAttribute("aria-expanded", "true");
  expect(api.mock.calls[1]?.[0]).toBe(
    `/api/v1/decisions/${decision.decisionId}/evidence/${passage.passageId}?runId=${runId}`,
  );
});

it("says when a passage cannot be read and keeps the decision", async () => {
  await openDecisions((url) =>
    url.includes("/evidence/") ? failure(503, "C10.UNAVAILABLE") : Response.json(decision),
  );
  const button = first(await screen.findAllByRole("button", { name: /^Evidence:/ }));

  await userEvent.click(button);

  expect(await screen.findByRole("alert")).toHaveTextContent("unavailable right now");
  expect(screen.getByText(first(decision.reasons).text)).toBeVisible();
});

it("marks evidence the record no longer holds", async () => {
  const evidenceStatus = [
    { refId: passage.passageId, status: "Missing" },
    ...decision.evidenceStatus.slice(1),
  ];
  await openDecisions(() => Response.json({ ...decision, evidenceStatus }));

  const text = `Evidence ${passage.passageId.slice(0, 8)} is missing from the record.`;
  expect(first(await screen.findAllByText(text))).toBeVisible();
});

it("shows an attempt that selected nothing", async () => {
  await openDecisions(() =>
    Response.json({
      ...decision,
      attemptOutcome: "DataUnavailable",
      selectedOption: null,
      consideredAlternatives: [],
      predictedEffects: null,
      subject: { ...decision.subject, type: "PlanningAttempt", version: null },
    }),
  );

  expect(await screen.findByText("Data unavailable")).toBeVisible();
  expect(screen.getByText("No option was selected.")).toBeVisible();
  expect(screen.getByRole("heading", { name: "Plan generated" })).toBeVisible();
  expect(screen.queryByText("Predicted effects")).not.toBeInTheDocument();
});

it("lets the operator choose between a plan's decisions", async () => {
  const replan = "2fcea9a4-54eb-43bc-80ce-d5620c8a6ca7";
  const { api } = await openDecisions(
    (url) =>
      url.includes("subjectId=")
        ? failure(409, "C10.DECISION_AMBIGUOUS", {
            candidates: [
              { decisionId: decision.decisionId, subjectVersion: 1 },
              { decisionId: replan, subjectVersion: 3 },
            ],
          })
        : Response.json({
            ...decision,
            decisionId: replan,
            decisionType: "Replan",
            relatedDecisionId: decision.decisionId,
            subject: { ...decision.subject, version: 3 },
          }),
    byPlan,
  );
  expect(await screen.findByRole("heading", { name: "This plan has 2 decisions" })).toBeVisible();

  await userEvent.click(screen.getByRole("link", { name: /Plan version 3/ }));

  expect(await screen.findByRole("heading", { name: "Replan · version 3" })).toBeVisible();
  const related = screen.getByRole("link", { name: "View the decision this one follows" });
  expect(related).toHaveAttribute("href", expect.stringContaining(decision.decisionId));
  expect(api.mock.calls.at(-1)?.[0]).toBe(`/api/v1/decisions/${replan}?runId=${runId}`);
});

it("finds a plan's decision from the form", async () => {
  const { router, api } = await openDecisions(() => Response.json(decision), "/decisions");
  const input = await screen.findByRole("textbox", { name: "Plan ID" });
  expect(api).not.toHaveBeenCalled();

  await userEvent.type(input, "not an ID");
  await userEvent.click(screen.getByRole("button", { name: "Find decisions" }));
  expect(screen.getByRole("alert")).toHaveTextContent("Enter a full plan ID");
  expect(api).not.toHaveBeenCalled();

  await userEvent.clear(input);
  await userEvent.type(input, ` ${decision.subject.id.toUpperCase()} `);
  await userEvent.click(screen.getByRole("button", { name: "Find decisions" }));

  expect(await screen.findByRole("heading", { name: /Plan generated/ })).toBeVisible();
  expect(router.state.location.search).toMatchObject({ planId: decision.subject.id });
  expect(first(api.mock.calls)[0]).toBe(
    `/api/v1/decisions?subjectType=Plan&subjectId=${decision.subject.id}&runId=${runId}`,
  );
});

it.each([
  [403, "C10.FORBIDDEN", "You don’t have access"],
  [404, "C10.DECISION_NOT_FOUND", "No decision was found"],
  [503, "C10.UNAVAILABLE", "unavailable right now"],
])("explains a %i and can retry", async (status, code, text) => {
  let failing = true;
  await openDecisions(() => (failing ? failure(status, code) : Response.json(decision)));
  expect(await screen.findByRole("alert")).toHaveTextContent(text);

  failing = false;
  await userEvent.click(screen.getByRole("button", { name: "Try again" }));

  expect(await screen.findByText(decision.selectedOption.summary)).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it.each([
  ["another decision", { ...decision, decisionId: scenario.correlationId }],
  ["another run", { ...decision, runScope: { mode: "Live", runId: null } }],
  ["a body it cannot read", { decisionId: decision.decisionId }],
])("does not show %s", async (_name, body) => {
  await openDecisions(() => Response.json(body));

  expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t read the decision record.");
  expect(screen.queryByText(decision.selectedOption.summary)).not.toBeInTheDocument();
});

it.each(["decisionId=abc", `planId=${decision.subject.id}&runId=abc`, "planId="])(
  "reports an incomplete link: %s",
  async (query) => {
    const { api } = await openDecisions(() => Response.json(decision), `/decisions?${query}`);

    expect(await screen.findByRole("alert")).toHaveTextContent("link is incomplete");
    expect(api).not.toHaveBeenCalled();
  },
);

it("is in the main navigation", async () => {
  await openDecisions(() => Response.json(decision));

  const nav = await screen.findByRole("navigation", { name: "Main" });
  expect(within(nav).getByRole("link", { name: /Decisions/ })).toBeInTheDocument();
});
