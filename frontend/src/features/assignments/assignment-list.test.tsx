import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AssignmentList } from "./assignment-list";

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

it("sends an operator offer and shows its persisted status after refresh", async () => {
  const offered = { ...row, offerId: "offer-1", status: "offered", canOffer: false };
  const request = vi
    .fn()
    .mockResolvedValueOnce(Response.json([row]))
    .mockResolvedValueOnce(Response.json({ code: "awaiting_acceptance" }))
    .mockResolvedValueOnce(Response.json([offered]));
  render(<AssignmentList providerId="provider-1" canRegister={false} request={request} />);
  await userEvent.click(await screen.findByRole("button", { name: "Send driver offer" }));
  expect(await screen.findByText("Awaiting driver response")).toBeVisible();
  expect(request).toHaveBeenCalledWith(
    "/vehicle-assignments",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ planId: row.planId, deliveryId: row.deliveryId, expectedVersion: 3 }),
      headers: expect.objectContaining({
        "X-Acting-Provider": "provider-1",
        "Idempotency-Key": expect.any(String),
      }),
    }),
  );
});

it("reuses a driver command key after an uncertain response and reloads acceptance", async () => {
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
    .mockResolvedValueOnce(Response.json([{ ...offered, status: "accepted", canRespond: false }]));
  render(<AssignmentList providerId="provider-1" canRegister={false} request={request} />);
  await userEvent.click(await screen.findByRole("button", { name: "Accept offer" }));
  expect(await screen.findByRole("alert")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "Accept offer" }));
  expect(await screen.findByText("Driver accepted")).toBeVisible();
  const mutations = request.mock.calls.filter(
    ([path]) => path === "/driver-offers/offer-1/response",
  );
  expect(mutations).toHaveLength(2);
  expect(mutations[0]?.[1].headers).toEqual(mutations[1]?.[1].headers);
  expect(mutations[0]?.[1].body).toBe(JSON.stringify({ response: "accept" }));
});
