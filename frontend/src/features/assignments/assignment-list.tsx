import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import type { ResourceScope } from "@/features/auth/resource-access";

interface Assignment {
  planId: string;
  planVersion: number;
  deliveryId: string;
  assignmentRef: string;
  offerId: string | null;
  resourceName: string;
  status: "allocated" | "offered" | "accepted" | "rejected";
  canOffer: boolean;
  canRespond: boolean;
}
const statusLabel = {
  allocated: "Vehicle allocated",
  offered: "Awaiting driver response",
  accepted: "Driver accepted",
  rejected: "Driver declined",
};
type Action = "offer" | "accept" | "reject";

export function AssignmentList({ providerId, request, runId }: ResourceScope & { runId?: string }) {
  const query = runId ? `?runId=${encodeURIComponent(runId)}` : "";
  const [rows, setRows] = useState<Assignment[]>([]);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const scope = useRef(new AbortController());
  const keys = useRef(new Map<string, string>());

  useEffect(() => {
    const controller = new AbortController();
    scope.current = controller;
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await request(`/vehicle-assignments${query}`, {
          headers: { "X-Acting-Provider": providerId },
          signal: controller.signal,
        });
        if (!response.ok)
          throw new Error(
            response.status === 403
              ? "You don’t have access to vehicle assignments."
              : "Couldn’t load vehicle assignments.",
          );
        const items: Assignment[] = await response.json();
        if (!controller.signal.aborted) setRows(items);
      } catch (err) {
        if (!controller.signal.aborted) {
          setRows([]);
          setError(err instanceof Error ? err.message : "Couldn’t load vehicle assignments.");
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    void load();
    return () => controller.abort();
  }, [providerId, request, revision, query]);

  function reload() {
    setLoading(true);
    setError("");
    setRevision((value) => value + 1);
  }

  async function submit(row: Assignment, action: Action) {
    const { signal } = scope.current;
    const key = row.assignmentRef + action;
    const idempotencyKey = keys.current.get(key) ?? crypto.randomUUID();
    keys.current.set(key, idempotencyKey);
    setBusy(true);
    setError("");
    try {
      const offer = action === "offer";
      const response = await request(
        offer ? `/vehicle-assignments${query}` : `/driver-offers/${row.offerId}/response${query}`,
        {
          method: "POST",
          signal,
          headers: {
            "X-Acting-Provider": providerId,
            "Content-Type": "application/json",
            "Idempotency-Key": idempotencyKey,
          },
          body: JSON.stringify(
            offer
              ? { planId: row.planId, deliveryId: row.deliveryId, expectedVersion: row.planVersion }
              : { response: action },
          ),
        },
      );
      signal.throwIfAborted();
      if (response.status === 403) {
        setRows([]);
        throw new Error("You don’t have access to this assignment.");
      }
      if (!response.ok)
        throw new Error(
          response.status === 409
            ? "This assignment has changed. Refresh to see its current status."
            : "Couldn’t confirm the result. Retry the action.",
        );
      keys.current.delete(key);
      reload();
    } catch (err) {
      if (!signal.aborted)
        setError(err instanceof Error ? err.message : "Couldn’t confirm the result.");
    } finally {
      if (!signal.aborted) setBusy(false);
    }
  }

  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="font-heading text-lg font-semibold">Vehicle assignments</h2>
        <Button variant="outline" onClick={reload} disabled={busy || loading}>
          Refresh
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
      {loading ? (
        <p role="status">Loading assignments…</p>
      ) : (
        !rows.length && !error && <p>No vehicle assignments for this provider.</p>
      )}
      {!loading &&
        rows.map((row) => (
          <Card key={row.assignmentRef}>
            <CardContent className="space-y-2">
              <h3 className="font-medium">{row.resourceName}</h3>
              <p>{statusLabel[row.status]}</p>
              <p className="text-sm text-muted-foreground">
                Delivery {row.deliveryId} · Plan version {row.planVersion}
              </p>
              {row.canOffer && (
                <Button disabled={busy} onClick={() => void submit(row, "offer")}>
                  Send driver offer
                </Button>
              )}
              {row.canRespond && (
                <div className="flex gap-2">
                  <Button disabled={busy} onClick={() => void submit(row, "accept")}>
                    Accept offer
                  </Button>
                  <Button
                    disabled={busy}
                    variant="outline"
                    onClick={() => void submit(row, "reject")}
                  >
                    Decline offer
                  </Button>
                </div>
              )}
            </CardContent>
          </Card>
        ))}
    </section>
  );
}
