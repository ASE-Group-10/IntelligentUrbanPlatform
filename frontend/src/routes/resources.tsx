import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { Boxes, RefreshCw } from "lucide-react";

import { PageHeader } from "@/components/layout/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

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

function ResourcesPage() {
  const [identity, setIdentity] = useState("dev-manager");
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
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  // Aborted whenever the identity changes, so work started as a previous identity is ignored.
  const sessionRef = useRef(new AbortController());

  const request = useCallback(
    async (path: string, init: RequestInit = {}) => {
      const response = await fetch(path, {
        ...init,
        headers: {
          Authorization: `Bearer ${identity}`,
          ...(init.headers ?? {}),
        },
      });
      const body = await response.json();
      if (!response.ok) {
        throw new Error(body.error?.message ?? "Request failed");
      }
      return body;
    },
    [identity],
  );

  useEffect(() => {
    sessionRef.current = new AbortController();
    return () => sessionRef.current.abort();
  }, []);

  function changeIdentity(next: string) {
    if (next === identity) return;
    // Invalidate work started as the previous identity and drop its data.
    sessionRef.current.abort();
    sessionRef.current = new AbortController();
    setResources([]);
    setIdentifier("");
    setName("");
    setMessage("");
    setError("");
    setSubmitting(false);
    setIdentity(next);
  }

  const refreshAbort = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    refreshAbort.current?.abort();
    const controller = new AbortController();
    refreshAbort.current = controller;
    setLoading(true);
    setError("");
    try {
      const data = await request("/api/v1/resources?limit=100", { signal: controller.signal });
      if (controller.signal.aborted) return;
      setResources(data.items);
    } catch (caught) {
      if (controller.signal.aborted) return;
      setError(caught instanceof Error ? caught.message : "Unable to load resources");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [request]);

  useEffect(() => {
    const controller = new AbortController();
    request("/api/v1/development/config", { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setLocations(data.locations);
      })
      .catch((caught) => {
        if (controller.signal.aborted) return;
        setError(caught instanceof Error ? caught.message : "Unable to load config");
      });
    return () => controller.abort();
  }, [request]);

  useEffect(() => {
    // Identity changed: drop the previous provider's data before loading the new one.
    void refresh();
    return () => refreshAbort.current?.abort();
  }, [refresh]);

  useEffect(() => {
    setDetails(JSON.stringify(detailsTemplate[resourceType], null, 2));
  }, [resourceType]);

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
      const resource = await request("/api/v1/resources", {
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
      await refresh();
    } catch (caught) {
      if (!isCurrent()) return;
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

  return (
    <div className="space-y-6">
      <PageHeader
        title="Resources"
        description="Register logistics resources and review their planning eligibility."
        actions={
          <div className="flex items-center gap-2">
            <label className="flex items-center gap-2 text-sm text-muted-foreground">
              Identity
              <select
                value={identity}
                onChange={(event) => changeIdentity(event.target.value)}
                className="h-8 rounded-lg border bg-background px-2 text-foreground"
              >
                <option value="dev-manager">Provider A · Manager</option>
                <option value="dev-operator">Provider A · Operator</option>
                <option value="dev-other-provider">Provider B · Manager</option>
              </select>
            </label>
            <Button
              variant="outline"
              size="icon"
              onClick={() => void refresh()}
              aria-label="Refresh resources"
            >
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
        <Card>
          <CardHeader>
            <CardTitle>Register resource</CardTitle>
          </CardHeader>
          <CardContent>
            <form className="space-y-4" onSubmit={registerResource}>
              <Field label="Resource type">
                <select
                  value={resourceType}
                  onChange={(event) => setResourceType(event.target.value as ResourceType)}
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
