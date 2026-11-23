import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AssignmentList } from "./assignment-list";

const demoRun = "c3f1a2b4-5d6e-4f70-8a9b-0c1d2e3f4a5b";
const row = {
  planId: "plan-3",
  deliveryId: "delivery-1",
  planVersion: 3,
  assignmentRef: "assignment-1",
  offerId: null,
  resourceName: "Dublin van",
  status: "allocated",
  canOffer: true,
  canRespond: false,
};

it.each([undefined, demoRun])(
  "sends an offer and refreshes in the same scope (%s)",
  async (runId) => {
    const path = runId ? `/vehicle-assignments?runId=${runId}` : "/vehicle-assignments";
    const offered = { ...row, offerId: "offer-1", status: "offered", canOffer: false };
    const request = vi
      .fn()
      .mockResolvedValueOnce(Response.json([row]))
      .mockResolvedValueOnce(Response.json({ code: "awaiting_acceptance" }))
      .mockResolvedValueOnce(Response.json([offered]));
    render(
      <AssignmentList
        runId={runId}
        providerId="provider-1"
        canRegister={false}
        request={request}
      />,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Send driver offer" }));
    expect(await screen.findByText("Awaiting driver response")).toBeVisible();
    expect(request.mock.calls.map(([url]) => url)).toEqual([path, path, path]);
    expect(request).toHaveBeenCalledWith(
      path,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          planId: row.planId,
          deliveryId: row.deliveryId,
          expectedVersion: 3,
        }),
        headers: expect.objectContaining({
          "X-Acting-Provider": "provider-1",
          "Idempotency-Key": expect.any(String),
        }),
      }),
    );
  },
);

it.each([undefined, demoRun])(
  "retries driver acceptance in the selected scope (%s)",
  async (runId) => {
    const offered = {
      ...row,
      offerId: "offer-1",
      status: "offered",
      canOffer: false,
      canRespond: true,
    };
    const request = vi
      .fn()
      .mockResolvedValueOnce(Response.json([offered]))
      .mockRejectedValueOnce(new Error("Connection lost"))
      .mockResolvedValueOnce(Response.json({ code: "assigned" }))
      .mockResolvedValueOnce(
        Response.json([{ ...offered, status: "accepted", canRespond: false }]),
      );
    render(
      <AssignmentList
        runId={runId}
        providerId="provider-1"
        canRegister={false}
        request={request}
      />,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Accept offer" }));
    expect(await screen.findByRole("alert")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Accept offer" }));
    expect(await screen.findByText("Driver accepted")).toBeVisible();
    const mutations = request.mock.calls.filter(
      ([path]) => path === `/driver-offers/offer-1/response${runId ? `?runId=${runId}` : ""}`,
    );
    expect(mutations).toHaveLength(2);
    expect(mutations[0]?.[1].headers).toEqual(mutations[1]?.[1].headers);
    expect(mutations[0]?.[1].body).toBe(JSON.stringify({ response: "accept" }));
  },
);

it("shows a released commitment on v4 without offer or acceptance actions", async () => {
  const request = vi
    .fn()
    .mockResolvedValueOnce(
      Response.json([
        { ...row, planVersion: 4, status: "unresolved", canOffer: false, canRespond: false },
      ]),
    );
  render(<AssignmentList providerId="provider-1" canRegister={false} request={request} />);
  expect(await screen.findByText("Revised plan needs a new allocation")).toBeVisible();
  expect(screen.getByText(/Plan version 4/)).toBeVisible();
  expect(
    screen.queryByRole("button", { name: /send driver offer|accept offer|decline offer/i }),
  ).toBeNull();
});
