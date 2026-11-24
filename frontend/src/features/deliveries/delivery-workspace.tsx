import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { Package, Pencil, RefreshCw } from "lucide-react";

import scenario from "../../../../fixtures/dublin-scenario.json";
import { PageHeader } from "@/components/layout/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuth } from "@/features/auth/auth-context";
import { ProviderPicker, type ResourceScope } from "@/features/auth/resource-access";
import { proposalLink } from "@/features/planning/proposal";

import {
  DELIVERY_STATUSES,
  dublinDemoDraft,
  DeliveryAccessDenied,
  DeliveryRequestError,
  VEHICLE_KINDS,
  draftFromDelivery,
  listDeliveries,
  submitDeliveryRequest,
  updateDeliveryRequest,
  validateDraft,
  type Delivery,
  type DeliveryDraft,
  type DeliveryStatus,
  type SubmitDeliveryResult,
  type VehicleKind,
} from "./deliveries";

const dublinTime = new Intl.DateTimeFormat("en-IE", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "Europe/Dublin",
});

const STATUS_LABELS: Record<DeliveryStatus, string> = {
  pending: "Pending",
  planned: "Planned",
  in_progress: "In progress",
  delivered: "Delivered",
  cancelled: "Cancelled",
};

const VEHICLE_LABELS: Record<VehicleKind, string> = {
  van: "Van",
  cargo_bike: "Cargo bike",
  truck: "Truck",
};

function formatDublinTime(iso: string): string {
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : dublinTime.format(parsed);
}

export function DeliveryWorkspace({ providerId, request: authenticatedRequest }: ResourceScope) {
  const { identity } = useAuth();
  const roles = identity?.memberships.find((entry) => entry.providerId === providerId)?.roles;
  const canReview = identity?.platformRoles.includes("admin") || roles?.includes("operator");
  const canSubmit = canReview || roles?.includes("manager");

  const [denied, setDenied] = useState(false);
  const [runId, setRunId] = useState<string>(scenario.runId);
  const [statusFilter, setStatusFilter] = useState<DeliveryStatus | "">("");
  const [deliveries, setDeliveries] = useState<Delivery[]>([]);
  const [loading, setLoading] = useState(true);
  const [listVersion, setListVersion] = useState(0);
  const [draft, setDraft] = useState<DeliveryDraft>(dublinDemoDraft);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [lastSubmitted, setLastSubmitted] = useState<SubmitDeliveryResult | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [retryPlanning, setRetryPlanning] = useState(false);
  const [editing, setEditing] = useState<Delivery | null>(null);
  const [editDraft, setEditDraft] = useState<DeliveryDraft | null>(null);
  const [editErrors, setEditErrors] = useState<Record<string, string>>({});
  const [savingEdit, setSavingEdit] = useState(false);

  const submission = useRef<{ idempotencyKey: string; correlationId: string } | null>(null);
  const editKeyRef = useRef<string>(crypto.randomUUID());
  const sessionRef = useRef(new AbortController());

  useEffect(() => {
    sessionRef.current = new AbortController();
    return () => sessionRef.current.abort();
  }, []);

  function refresh() {
    setLoading(true);
    setError("");
    setListVersion((v) => v + 1);
  }

  function startNewSubmission() {
    submission.current = null;
    setRetryPlanning(false);
  }

  function updateDraft<K extends keyof DeliveryDraft>(field: K, value: DeliveryDraft[K]) {
    setDraft((prev) => ({ ...prev, [field]: value }));
    setFieldErrors((prev) => {
      if (!(field in prev)) return prev;
      return Object.fromEntries(Object.entries(prev).filter(([key]) => key !== field));
    });
    startNewSubmission();
  }

  function toggleVehicleKind(kind: VehicleKind) {
    const exists = draft.vehicleKinds.includes(kind);
    const next = exists
      ? draft.vehicleKinds.filter((item) => item !== kind)
      : [...draft.vehicleKinds, kind];
    updateDraft("vehicleKinds", next);
  }

  useEffect(() => {
    const controller = new AbortController();
    listDeliveries(authenticatedRequest, providerId, {
      runId,
      status: statusFilter,
      signal: controller.signal,
    })
      .then((page) => {
        if (!controller.signal.aborted) {
          setDeliveries(page.items);
          setError("");
        }
      })
      .catch((caught) => {
        if (controller.signal.aborted) return;
        if (caught instanceof DeliveryAccessDenied) {
          setDenied(true);
        } else {
          setDeliveries([]);
          setError(caught instanceof Error ? caught.message : "Unable to load deliveries");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [authenticatedRequest, providerId, runId, statusFilter, listVersion]);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const session = sessionRef.current;
    if (submitting) return;
    const isCurrent = () => !session.signal.aborted;

    const validated = validateDraft(draft);
    if (validated.errors) {
      setFieldErrors(validated.errors);
      setError("Correct the highlighted fields.");
      return;
    }

    setFieldErrors({});
    setError("");
    setSubmitting(true);
    try {
      submission.current ??= {
        idempotencyKey: crypto.randomUUID(),
        correlationId: crypto.randomUUID(),
      };
      const command = submission.current;
      const result = await submitDeliveryRequest(
        authenticatedRequest,
        providerId,
        validated.payload,
        {
          ...command,
          runId,
          signal: session.signal,
        },
      );
      if (!isCurrent()) return;
      setLastSubmitted(result);
      if (submission.current === command) {
        if (result.runId && !result.planId) setRetryPlanning(true);
        else startNewSubmission();
      }
      refresh();
    } catch (caught) {
      if (!isCurrent()) return;
      if (caught instanceof DeliveryAccessDenied) {
        setDenied(true);
        return;
      }
      if (caught instanceof DeliveryRequestError) {
        setFieldErrors(caught.fieldErrors);
        setError(caught.message);
      } else {
        setError(caught instanceof Error ? caught.message : "Unable to submit delivery");
      }
    } finally {
      if (isCurrent()) setSubmitting(false);
    }
  }

  function startEdit(delivery: Delivery) {
    setEditing(delivery);
    setEditDraft(draftFromDelivery(delivery));
    setEditErrors({});
    editKeyRef.current = crypto.randomUUID();
  }

  async function handleSaveEdit(event: FormEvent) {
    event.preventDefault();
    if (!editing || !editDraft || savingEdit) return;
    const session = sessionRef.current;
    const isCurrent = () => !session.signal.aborted;

    const validated = validateDraft(editDraft);
    if (validated.errors) {
      setEditErrors(validated.errors);
      return;
    }
    setEditErrors({});
    setSavingEdit(true);
    try {
      await updateDeliveryRequest(
        authenticatedRequest,
        providerId,
        editing.deliveryId,
        validated.payload,
        {
          expectedVersion: editing.version,
          idempotencyKey: editKeyRef.current,
          correlationId: crypto.randomUUID(),
          runId,
          signal: session.signal,
        },
      );
      if (!isCurrent()) return;
      setEditing(null);
      setEditDraft(null);
      refresh();
    } catch (caught) {
      if (!isCurrent()) return;
      if (caught instanceof DeliveryAccessDenied) {
        setDenied(true);
        return;
      }
      if (caught instanceof DeliveryRequestError) {
        setEditErrors(caught.fieldErrors);
        setError(caught.message);
      } else {
        setError(caught instanceof Error ? caught.message : "Unable to update delivery");
      }
    } finally {
      if (isCurrent()) setSavingEdit(false);
    }
  }

  if (denied) {
    return (
      <div className="space-y-3 p-4">
        <p role="alert" className="text-destructive">
          Access denied for this provider.
        </p>
        <ProviderPicker />
      </div>
    );
  }

  const review =
    canReview && lastSubmitted && !lastSubmitted.planStub
      ? proposalLink.safeParse({
          planId: lastSubmitted.planId,
          runId: lastSubmitted.runId,
          correlationId: lastSubmitted.correlationId,
        }).data
      : undefined;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Deliveries"
        description="Submit validated delivery requests and track their status and progress."
        actions={
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-sm">
              Delivery scope
              <select
                className="rounded-lg border border-input bg-background px-2 py-1"
                value={runId}
                disabled={submitting}
                onChange={(event) => {
                  setLoading(true);
                  setRunId(event.target.value);
                  setLastSubmitted(null);
                  startNewSubmission();
                }}
              >
                <option value={scenario.runId}>Dublin simulation</option>
                <option value="">Live</option>
              </select>
            </label>
            <ProviderPicker />
            <Button variant="outline" size="icon" onClick={refresh} aria-label="Refresh deliveries">
              <RefreshCw className={loading ? "animate-spin" : ""} />
            </Button>
          </div>
        }
      />

      {lastSubmitted && lastSubmitted.runId === (runId || null) ? (
        <div
          role="status"
          className="rounded-lg border border-status-open/30 bg-status-open/10 px-4 py-3 text-sm"
        >
          <p className="font-medium">
            Delivery {lastSubmitted.delivery.deliveryId} submitted (v
            {lastSubmitted.delivery.version})
          </p>
          {review && (
            <Link to="/plans" search={review} className="font-medium text-primary underline">
              Review plan
            </Link>
          )}
          {retryPlanning && (
            <p>The delivery is saved. Use Retry planning to request its proposal again.</p>
          )}
          <p className="mt-1 text-xs text-muted-foreground">
            {lastSubmitted.correlationId ? `Correlation ID: ${lastSubmitted.correlationId}` : null}
            {lastSubmitted.planId
              ? ` · Plan: ${lastSubmitted.planId} (v${lastSubmitted.planVersion ?? "1"}${lastSubmitted.planStub ? ", C05 stub" : ""})`
              : null}
          </p>
        </div>
      ) : null}

      {error ? (
        <div
          role="alert"
          className="rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
        >
          {error}
        </div>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-[420px_minmax(0,1fr)]">
        {canSubmit ? (
          <Card>
            <CardHeader className="flex flex-row items-center justify-between gap-2">
              <CardTitle>Submit delivery request</CardTitle>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => {
                  setDraft(dublinDemoDraft());
                  setFieldErrors({});
                  setError("");
                  startNewSubmission();
                }}
              >
                Dublin preset
              </Button>
            </CardHeader>
            <CardContent>
              <form className="space-y-4" onSubmit={handleSubmit} noValidate>
                <fieldset className="space-y-2 rounded-lg border p-3">
                  <legend className="px-1 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
                    Pickup
                  </legend>
                  <FormField label="Pickup address" error={fieldErrors.pickupAddress}>
                    <input
                      value={draft.pickupAddress}
                      onChange={(e) => updateDraft("pickupAddress", e.target.value)}
                      placeholder="Dublin Port"
                      aria-invalid={Boolean(fieldErrors.pickupAddress)}
                    />
                  </FormField>
                  <div className="grid grid-cols-2 gap-2">
                    <FormField label="Pickup latitude" error={fieldErrors.pickupLatitude}>
                      <input
                        value={draft.pickupLatitude}
                        onChange={(e) => updateDraft("pickupLatitude", e.target.value)}
                        inputMode="decimal"
                        aria-invalid={Boolean(fieldErrors.pickupLatitude)}
                      />
                    </FormField>
                    <FormField label="Pickup longitude" error={fieldErrors.pickupLongitude}>
                      <input
                        value={draft.pickupLongitude}
                        onChange={(e) => updateDraft("pickupLongitude", e.target.value)}
                        inputMode="decimal"
                        aria-invalid={Boolean(fieldErrors.pickupLongitude)}
                      />
                    </FormField>
                  </div>
                </fieldset>

                <fieldset className="space-y-2 rounded-lg border p-3">
                  <legend className="px-1 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
                    Destination
                  </legend>
                  <FormField label="Destination address" error={fieldErrors.destinationAddress}>
                    <input
                      value={draft.destinationAddress}
                      onChange={(e) => updateDraft("destinationAddress", e.target.value)}
                      placeholder="Trinity College Dublin"
                      aria-invalid={Boolean(fieldErrors.destinationAddress)}
                    />
                  </FormField>
                  <div className="grid grid-cols-2 gap-2">
                    <FormField label="Destination latitude" error={fieldErrors.destinationLatitude}>
                      <input
                        value={draft.destinationLatitude}
                        onChange={(e) => updateDraft("destinationLatitude", e.target.value)}
                        inputMode="decimal"
                        aria-invalid={Boolean(fieldErrors.destinationLatitude)}
                      />
                    </FormField>
                    <FormField
                      label="Destination longitude"
                      error={fieldErrors.destinationLongitude}
                    >
                      <input
                        value={draft.destinationLongitude}
                        onChange={(e) => updateDraft("destinationLongitude", e.target.value)}
                        inputMode="decimal"
                        aria-invalid={Boolean(fieldErrors.destinationLongitude)}
                      />
                    </FormField>
                  </div>
                </fieldset>

                <fieldset className="space-y-2 rounded-lg border p-3">
                  <legend className="px-1 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
                    Cargo &amp; time window (UTC)
                  </legend>
                  <FormField label="Cargo description" error={fieldErrors.cargoDescription}>
                    <input
                      value={draft.cargoDescription}
                      onChange={(e) => updateDraft("cargoDescription", e.target.value)}
                      placeholder="Office supplies"
                      aria-invalid={Boolean(fieldErrors.cargoDescription)}
                    />
                  </FormField>
                  <div className="grid grid-cols-2 gap-2">
                    <FormField label="Weight (kg)" error={fieldErrors.weightKg}>
                      <input
                        value={draft.weightKg}
                        onChange={(e) => updateDraft("weightKg", e.target.value)}
                        inputMode="decimal"
                        aria-invalid={Boolean(fieldErrors.weightKg)}
                      />
                    </FormField>
                    <FormField label="Parcels" error={fieldErrors.parcelCount}>
                      <input
                        value={draft.parcelCount}
                        onChange={(e) => updateDraft("parcelCount", e.target.value)}
                        inputMode="numeric"
                        aria-invalid={Boolean(fieldErrors.parcelCount)}
                      />
                    </FormField>
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    <FormField label="Window start (UTC)" error={fieldErrors.windowStart}>
                      <input
                        value={draft.windowStart}
                        onChange={(e) => updateDraft("windowStart", e.target.value)}
                        aria-invalid={Boolean(fieldErrors.windowStart)}
                      />
                    </FormField>
                    <FormField label="Window end (UTC)" error={fieldErrors.windowEnd}>
                      <input
                        value={draft.windowEnd}
                        onChange={(e) => updateDraft("windowEnd", e.target.value)}
                        aria-invalid={Boolean(fieldErrors.windowEnd)}
                      />
                    </FormField>
                  </div>
                </fieldset>

                <fieldset className="space-y-2 rounded-lg border p-3">
                  <legend className="px-1 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
                    Constraints
                  </legend>
                  <div className="space-y-1">
                    <span className="text-sm font-medium">Permitted vehicle types</span>
                    <div className="flex flex-wrap gap-3 pt-1">
                      {VEHICLE_KINDS.map((kind) => (
                        <label key={kind} className="flex items-center gap-1.5 text-sm">
                          <input
                            type="checkbox"
                            checked={draft.vehicleKinds.includes(kind)}
                            onChange={() => toggleVehicleKind(kind)}
                          />
                          {VEHICLE_LABELS[kind]}
                        </label>
                      ))}
                    </div>
                    {fieldErrors.vehicleKinds ? (
                      <p className="text-xs text-destructive">{fieldErrors.vehicleKinds}</p>
                    ) : null}
                  </div>
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={draft.fragile}
                      onChange={(e) => updateDraft("fragile", e.target.checked)}
                    />
                    Fragile cargo
                  </label>
                  <div className="grid grid-cols-2 gap-2">
                    <FormField label="Min temp (°C, optional)" error={fieldErrors.tempMin}>
                      <input
                        value={draft.tempMin}
                        onChange={(e) => updateDraft("tempMin", e.target.value)}
                        placeholder="e.g. 2"
                        inputMode="decimal"
                        aria-invalid={Boolean(fieldErrors.tempMin)}
                      />
                    </FormField>
                    <FormField label="Max temp (°C, optional)" error={fieldErrors.tempMax}>
                      <input
                        value={draft.tempMax}
                        onChange={(e) => updateDraft("tempMax", e.target.value)}
                        placeholder="e.g. 8"
                        inputMode="decimal"
                        aria-invalid={Boolean(fieldErrors.tempMax)}
                      />
                    </FormField>
                  </div>
                </fieldset>

                <Button type="submit" className="w-full" disabled={submitting}>
                  {submitting
                    ? "Submitting…"
                    : retryPlanning
                      ? "Retry planning"
                      : "Submit delivery"}
                </Button>
              </form>
            </CardContent>
          </Card>
        ) : (
          <p className="text-sm text-muted-foreground">
            You don’t have permission to submit delivery requests for this provider.
          </p>
        )}

        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="font-heading text-base font-semibold">Delivery requests</h2>
              <span className="text-sm text-muted-foreground">{deliveries.length} saved</span>
            </div>
            <label className="flex items-center gap-2 text-sm">
              Status filter
              <select
                className="rounded-lg border border-input bg-background px-2 py-1"
                value={statusFilter}
                onChange={(e) => {
                  setLoading(true);
                  setStatusFilter(e.target.value as DeliveryStatus | "");
                }}
              >
                <option value="">All statuses</option>
                {DELIVERY_STATUSES.map((status) => (
                  <option key={status} value={status}>
                    {STATUS_LABELS[status]}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {loading ? <p className="text-sm text-muted-foreground">Loading deliveries…</p> : null}

          {!loading && deliveries.length === 0 ? (
            <Card>
              <CardContent className="flex min-h-40 items-center justify-center text-muted-foreground">
                <div className="text-center">
                  <Package className="mx-auto mb-2 size-6" />
                  <p>No delivery requests in this scope yet.</p>
                </div>
              </CardContent>
            </Card>
          ) : null}

          {deliveries.map((delivery) => {
            const isEditing = editing?.deliveryId === delivery.deliveryId && editDraft !== null;
            return (
              <Card key={delivery.deliveryId}>
                <CardContent className="space-y-3">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <p className="font-medium">
                        {delivery.pickup.address} → {delivery.destination.address}
                      </p>
                      <p className="text-sm text-muted-foreground">
                        {delivery.cargo.description} · {delivery.cargo.weightKg} kg ·{" "}
                        {delivery.cargo.parcelCount}{" "}
                        {delivery.cargo.parcelCount === 1 ? "parcel" : "parcels"} · v
                        {delivery.version}
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      <Badge variant={delivery.status === "delivered" ? "default" : "secondary"}>
                        {STATUS_LABELS[delivery.status]}
                      </Badge>
                      {canSubmit && delivery.status === "pending" && !isEditing ? (
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() => startEdit(delivery)}
                        >
                          <Pencil className="mr-1 size-3.5" />
                          Edit
                        </Button>
                      ) : null}
                    </div>
                  </div>

                  <p className="text-xs text-muted-foreground">
                    Window (Dublin): {formatDublinTime(delivery.timeWindow.start)} –{" "}
                    {formatDublinTime(delivery.timeWindow.end)} · ID{" "}
                    <span className="font-mono">{delivery.deliveryId.slice(0, 8)}</span>
                  </p>

                  <div className="flex flex-wrap gap-2">
                    <Badge variant="outline">Progress: {delivery.progress.status}</Badge>
                    {delivery.constraints.vehicleKinds.map((kind) => (
                      <Badge key={kind} variant="outline">
                        {VEHICLE_LABELS[kind]}
                      </Badge>
                    ))}
                    {delivery.constraints.fragile ? <Badge variant="outline">Fragile</Badge> : null}
                    {delivery.constraints.temperatureRangeC ? (
                      <Badge variant="outline">
                        {delivery.constraints.temperatureRangeC.minimum}–
                        {delivery.constraints.temperatureRangeC.maximum} °C
                      </Badge>
                    ) : null}
                  </div>

                  {isEditing ? (
                    <form
                      onSubmit={handleSaveEdit}
                      className="space-y-3 rounded-lg border bg-muted/30 p-3"
                    >
                      <p className="text-xs font-medium text-muted-foreground">
                        Editing pending delivery (expected version {editing.version})
                      </p>
                      <FormField label="Cargo description" error={editErrors.cargoDescription}>
                        <input
                          value={editDraft.cargoDescription}
                          onChange={(e) =>
                            setEditDraft({ ...editDraft, cargoDescription: e.target.value })
                          }
                        />
                      </FormField>
                      <div className="grid grid-cols-2 gap-2">
                        <FormField label="Weight (kg)" error={editErrors.weightKg}>
                          <input
                            value={editDraft.weightKg}
                            onChange={(e) =>
                              setEditDraft({ ...editDraft, weightKg: e.target.value })
                            }
                          />
                        </FormField>
                        <FormField label="Parcels" error={editErrors.parcelCount}>
                          <input
                            value={editDraft.parcelCount}
                            onChange={(e) =>
                              setEditDraft({ ...editDraft, parcelCount: e.target.value })
                            }
                          />
                        </FormField>
                      </div>
                      <div className="flex gap-2">
                        <Button type="submit" size="sm" disabled={savingEdit}>
                          {savingEdit ? "Saving…" : "Save changes"}
                        </Button>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            setEditing(null);
                            setEditDraft(null);
                          }}
                        >
                          Cancel
                        </Button>
                      </div>
                    </form>
                  ) : null}
                </CardContent>
              </Card>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function FormField({
  label,
  error,
  children,
}: {
  label: string;
  error?: string;
  children: ReactNode;
}) {
  return (
    <label className="grid gap-1 text-sm font-medium">
      {label}
      <span className="[&>input]:py-1.5 [&>input,&>select]:w-full [&>input,&>select]:rounded-lg [&>input,&>select]:border [&>input,&>select]:bg-background [&>input,&>select]:px-3 [&>select]:h-9">
        {children}
      </span>
      {error ? <span className="text-xs font-normal text-destructive">{error}</span> : null}
    </label>
  );
}
