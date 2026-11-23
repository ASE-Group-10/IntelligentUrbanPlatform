import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { Boxes, RefreshCw } from "lucide-react";

import { PageHeader } from "@/components/layout/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

import {
  ProviderPicker,
  ResourceAccess,
  type ResourceScope,
} from "@/features/auth/resource-access";

type ResourceType = "vehicle" | "depot" | "warehouse" | "locker";

interface Resource {
  id: string;
  external_identifier: string;
  name: string;
  resource_type: ResourceType;
  profile_status: "active" | "inactive";
  observed_availability: "available" | "unavailable" | "unknown";
  agent_status: "pending" | "active" | "failed";
  planning_eligible: boolean;
  eligibility_reasons: string[];
  version: number;
  address: string;
  latitude: number;
  longitude: number;
  details: Record<string, unknown>;
}

interface LocationOption {
  address: string;
  latitude: number;
  longitude: number;
}

const detailsTemplate: Record<ResourceType, Record<string, unknown>> = {
  vehicle: {
    timezone: "Europe/Dublin",
    operating_hours: [],
    restrictions: [],
    vehicle_category: "van",
    max_weight_kg: 800,
    max_volume_m3: 6,
    powertrain: "electric",
    autonomous: false,
  },
  depot: {
    timezone: "Europe/Dublin",
    operating_hours: [],
    restrictions: [],
    storage_capacity_m3: 250,
    loading_bays: 2,
    supported_vehicle_categories: ["van", "cargo_bike"],
  },
  warehouse: {
    timezone: "Europe/Dublin",
    operating_hours: [],
    restrictions: [],
    storage_capacity_m3: 1000,
    handling_capabilities: ["ambient", "pallets"],
  },
  locker: {
    timezone: "Europe/Dublin",
    operating_hours: [],
    restrictions: [],
    public_access: true,
    max_parcel_weight_kg: 20,
    compartments: [{ size: "small", count: 12, length_cm: 40, width_cm: 30, height_cm: 20 }],
  },
};

class ProviderAccessDenied extends Error {}

function ResourcesPage() {
  return <ResourceAccess>{(scope) => <ResourceWorkspace {...scope} />}</ResourceAccess>;
}

function ResourceWorkspace({
  providerId,
  canRegister,
  request: authenticatedRequest,
}: ResourceScope) {
  const [denied, setDenied] = useState(false);
  const [locations, setLocations] = useState<LocationOption[]>([]);
  const [resources, setResources] = useState<Resource[]>([]);
  const [resourceType, setResourceType] = useState<ResourceType>("vehicle");
  const [details, setDetails] = useState(JSON.stringify(detailsTemplate.vehicle, null, 2));
  const [identifier, setIdentifier] = useState("");
  const [name, setName] = useState("");
  const [locationIndex, setLocationIndex] = useState(0);
  const [availability, setAvailability] = useState("available");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [listVersion, setListVersion] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  // Aborted whenever the identity changes, so work started as a previous identity is ignored.
  const sessionRef = useRef(new AbortController());

  const request = useCallback(
    async (path: string, init: RequestInit = {}) => {
      const headers = new Headers(init.headers);
      headers.set("X-Acting-Provider", providerId);
      const response = await authenticatedRequest(path, { ...init, headers });
      init.signal?.throwIfAborted();
      if (response.status === 403) {
        throw new ProviderAccessDenied();
      }
      const body = await response.json();
      if (!response.ok) {
        throw new Error(body.error?.message ?? "Request failed");
      }
      return body;
    },
    [authenticatedRequest, providerId],
  );

  useEffect(() => {
    sessionRef.current = new AbortController();
    return () => sessionRef.current.abort();
  }, []);

  function refresh() {
    setLoading(true);
    setError("");
    setListVersion((version) => version + 1);
  }

  useEffect(() => {
    const controller = new AbortController();
    request("/resources?limit=100", { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setResources(data.items);
      })
      .catch((caught) => {
        if (controller.signal.aborted) return;
        if (caught instanceof ProviderAccessDenied) setDenied(true);
        else setError(caught instanceof Error ? caught.message : "Unable to load resources");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [request, listVersion]);

  useEffect(() => {
    const controller = new AbortController();
    request("/development/config", { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setLocations(data.locations);
      })
      .catch((caught) => {
        if (controller.signal.aborted) return;
        if (caught instanceof ProviderAccessDenied) setDenied(true);
        else setError(caught instanceof Error ? caught.message : "Unable to load config");
      });
    return () => controller.abort();
  }, [request]);

  async function registerResource(event: FormEvent) {
    event.preventDefault();
    const session = sessionRef.current;
    if (submitting) return;
    const isCurrent = () => !session.signal.aborted;
    setError("");
    setMessage("");
    setSubmitting(true);
    try {
      const location = locations[locationIndex];
      if (!location) throw new Error("Choose a location");
      const parsedDetails = JSON.parse(details);
      const resource = await request("/resources", {
        method: "POST",
        signal: session.signal,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({
          external_identifier: identifier,
          name,
          resource_type: resourceType,
          address: location.address,
          latitude: location.latitude,
          longitude: location.longitude,
          profile_status: "active",
          observed_availability: availability,
          details: parsedDetails,
        }),
      });
      if (!isCurrent()) return;
      setMessage(`${resource.name} registered successfully`);
      setIdentifier("");
      setName("");
      refresh();
    } catch (caught) {
      if (!isCurrent()) return;
      if (caught instanceof ProviderAccessDenied) {
        setDenied(true);
        return;
      }
      setError(
        caught instanceof SyntaxError
          ? "Details must be valid JSON"
          : caught instanceof Error
            ? caught.message
            : "Unable to register resource",
      );
    } finally {
      if (isCurrent()) setSubmitting(false);
    }
  }

  if (denied)
    return (
      <div className="space-y-3 p-4">
        <p role="alert" className="text-destructive">
          Access denied for this provider.
        </p>
        <ProviderPicker />
      </div>
    );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Resources"
        description="Register logistics resources and review their planning eligibility."
        actions={
          <div className="flex items-center gap-3">
            <ProviderPicker />
            <Button variant="outline" size="icon" onClick={refresh} aria-label="Refresh resources">
              <RefreshCw className={loading ? "animate-spin" : ""} />
            </Button>
          </div>
        }
      />

      {message ? (
        <div className="rounded-lg border border-status-open/30 bg-status-open/10 px-4 py-3 text-sm">
          {message}
        </div>
      ) : null}
      {error ? (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-[380px_minmax(0,1fr)]">
        {canRegister ? (
          <Card>
            <CardHeader>
              <CardTitle>Register resource</CardTitle>
            </CardHeader>
            <CardContent>
              <form className="space-y-4" onSubmit={registerResource}>
                <Field label="Resource type">
                  <select
                    value={resourceType}
                    onChange={(event) => {
                      const next = event.target.value as ResourceType;
                      setResourceType(next);
                      setDetails(JSON.stringify(detailsTemplate[next], null, 2));
                    }}
                  >
                    <option value="vehicle">Vehicle</option>
                    <option value="depot">Depot</option>
                    <option value="warehouse">Warehouse</option>
                    <option value="locker">Locker</option>
                  </select>
                </Field>
                <Field label="Identifier">
                  <input
                    required
                    value={identifier}
                    onChange={(event) => setIdentifier(event.target.value)}
                    placeholder="VAN-001"
                  />
                </Field>
                <Field label="Name">
                  <input
                    required
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                    placeholder="Dublin electric van"
                  />
                </Field>
                <Field label="Location">
                  <select
                    value={locationIndex}
                    onChange={(event) => setLocationIndex(Number(event.target.value))}
                  >
                    {locations.map((location, index) => (
                      <option key={location.address} value={index}>
                        {location.address}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Observed availability">
                  <select
                    value={availability}
                    onChange={(event) => setAvailability(event.target.value)}
                  >
                    <option value="available">Available</option>
                    <option value="unavailable">Unavailable</option>
                    <option value="unknown">Unknown</option>
                  </select>
                </Field>
                <Field label="Type-specific details (JSON)">
                  <textarea
                    value={details}
                    onChange={(event) => setDetails(event.target.value)}
                    rows={12}
                  />
                </Field>
                <Button type="submit" className="w-full" disabled={submitting}>
                  Register resource
                </Button>
              </form>
            </CardContent>
          </Card>
        ) : (
          <p>You don’t have permission to register resources for this provider.</p>
        )}

        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="font-heading text-base font-semibold">Provider resources</h2>
            <span className="text-sm text-muted-foreground">{resources.length} saved</span>
          </div>
          {loading ? <p className="text-sm text-muted-foreground">Loading resources…</p> : null}
          {!loading && resources.length === 0 ? (
            <Card>
              <CardContent className="flex min-h-40 items-center justify-center text-muted-foreground">
                <div className="text-center">
                  <Boxes className="mx-auto mb-2 size-6" />
                  <p>No resources yet.</p>
                </div>
              </CardContent>
            </Card>
          ) : null}
          {resources.map((resource) => (
            <Card key={resource.id}>
              <CardContent className="space-y-2">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <p className="font-medium">{resource.name}</p>
                    <p className="text-sm text-muted-foreground">
                      {resource.external_identifier} · {resource.resource_type} · v
                      {resource.version}
                    </p>
                  </div>
                  <Badge variant={resource.planning_eligible ? "default" : "secondary"}>
                    {resource.planning_eligible ? "Planning eligible" : "Not eligible"}
                  </Badge>
                </div>
                <div className="flex flex-wrap gap-2">
                  <Badge variant="outline">Agent {resource.agent_status}</Badge>
                  <Badge variant="outline">{resource.profile_status}</Badge>
                  <Badge variant="outline">{resource.observed_availability}</Badge>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="grid gap-1.5 text-sm font-medium">
      {label}
      <span className="[&>input,&>select,&>textarea]:w-full [&>input,&>select,&>textarea]:rounded-lg [&>input,&>select,&>textarea]:border [&>input,&>select,&>textarea]:bg-background [&>input,&>select,&>textarea]:px-3 [&>input,&>textarea]:py-2 [&>select]:h-9 [&>textarea]:font-mono [&>textarea]:text-xs">
        {children}
      </span>
    </label>
  );
}

export const Route = createFileRoute("/resources")({
  component: ResourcesPage,
});
