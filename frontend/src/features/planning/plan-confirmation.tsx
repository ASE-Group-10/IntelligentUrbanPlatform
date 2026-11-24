import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import type { ResourceScope } from "@/features/auth/resource-access";
import { findDemoSegment } from "@/features/city-monitoring/demo-segments";

import { PlanError, requestPlan, type Plan, type ProposalLink } from "./proposal";

export function PlanConfirmation({
  providerId,
  request,
  link,
  onConfirmed,
}: Pick<ResourceScope, "providerId" | "request"> & {
  link: ProposalLink;
  onConfirmed: (version: number) => void;
}) {
  const [plan, setPlan] = useState<Plan>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const confirmation = useRef<AbortController | null>(null);

  useEffect(() => {
    const read = new AbortController();
    requestPlan(request, providerId, link, read.signal)
      .then((value) => {
        if (read.signal.aborted) return;
        setPlan(value);
        if (value.status === "confirmed") onConfirmed(value.version);
      })
      .catch((err: unknown) => {
        if (!read.signal.aborted)
          setError(err instanceof PlanError ? err.message : "Couldn’t load this plan.");
      });
    return () => {
      read.abort();
      confirmation.current?.abort();
      confirmation.current = null;
    };
  }, [request, providerId, link, revision, onConfirmed]);

  async function confirm() {
    if (!plan || plan.status !== "awaiting_confirmation" || confirmation.current) return;
    const controller = new AbortController();
    confirmation.current = controller;
    setBusy(true);
    setError("");
    try {
      const confirmed = await requestPlan(
        request,
        providerId,
        link,
        controller.signal,
        plan.version,
      );
      if (!controller.signal.aborted) {
        setPlan(confirmed);
        onConfirmed(confirmed.version);
      }
    } catch (err) {
      if (controller.signal.aborted) return;
      if (err instanceof PlanError && [400, 401, 403, 404, 409].includes(err.status)) {
        setPlan(undefined);
        setError(err.message);
      } else {
        setError("Couldn’t verify confirmation. Retry Confirm plan to recover the result.");
      }
    } finally {
      if (!controller.signal.aborted) {
        confirmation.current = null;
        setBusy(false);
      }
    }
  }

  return (
    <section aria-labelledby="proposal-heading" className="space-y-3">
      <h2 id="proposal-heading" className="font-heading text-lg font-semibold">
        Delivery plan
      </h2>
      <Card>
        <CardContent className="space-y-2">
          {plan && (
            <>
              <p role="status" className="font-medium">
                {plan.status === "confirmed" ? "Plan confirmed" : "Awaiting confirmation"} · Version{" "}
                {plan.version}
              </p>
              <p className="text-sm break-words">Plan ID {plan.planId}</p>
              <p className="text-sm break-words">Delivery {plan.requestIds.join(", ")}</p>
              {plan.routes.map((route, index) => (
                <p key={index} className="text-sm text-muted-foreground">
                  Route:{" "}
                  {route.segmentKeys.map((key) => findDemoSegment(key)?.name ?? key).join(" → ")}
                </p>
              ))}
            </>
          )}
          {error && (
            <p role="alert" className="text-destructive">
              {error}
            </p>
          )}
          {!plan && !error && <p role="status">Loading delivery plan…</p>}
          {!plan && error && (
            <Button
              variant="outline"
              onClick={() => {
                setError("");
                setRevision((value) => value + 1);
              }}
            >
              Reload plan
            </Button>
          )}
          {plan?.status === "awaiting_confirmation" && (
            <Button disabled={busy} onClick={() => void confirm()}>
              {busy ? "Confirming…" : "Confirm plan"}
            </Button>
          )}
        </CardContent>
      </Card>
    </section>
  );
}
