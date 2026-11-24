import { z } from "zod";

import type { SessionRequest } from "@/features/auth/identity";

export const VEHICLE_KINDS = ["van", "cargo_bike", "truck"] as const;
export type VehicleKind = (typeof VEHICLE_KINDS)[number];

export const DELIVERY_STATUSES = [
  "pending",
  "planned",
  "in_progress",
  "delivered",
  "cancelled",
] as const;
export type DeliveryStatus = (typeof DELIVERY_STATUSES)[number];

export const PROGRESS_STATUSES = [
  "pending",
  "picked_up",
  "in_transit",
  "delivered",
  "failed",
] as const;
export type ProgressStatus = (typeof PROGRESS_STATUSES)[number];

const locationSchema = z.object({
  address: z.string().min(1),
  latitude: z.number(),
  longitude: z.number(),
  instructions: z.string().nullable().optional(),
});

const cargoSchema = z.object({
  description: z.string().min(1),
  weightKg: z.number().positive(),
  parcelCount: z.number().int().min(1),
});

const timeWindowSchema = z.object({
  start: z.string().min(1),
  end: z.string().min(1),
});

const temperatureRangeSchema = z.object({
  minimum: z.number(),
  maximum: z.number(),
});

const constraintsSchema = z.object({
  vehicleKinds: z.array(z.enum(VEHICLE_KINDS)).min(1),
  fragile: z.boolean(),
  temperatureRangeC: temperatureRangeSchema.nullable().optional(),
});

const progressSchema = z.object({
  status: z.enum(PROGRESS_STATUSES),
  updatedAt: z.string().min(1),
  note: z.string().nullable().optional(),
});

export const deliverySchema = z.object({
  deliveryId: z.string().uuid(),
  version: z.number().int().min(1),
  status: z.enum(DELIVERY_STATUSES),
  pickup: locationSchema,
  destination: locationSchema,
  cargo: cargoSchema,
  timeWindow: timeWindowSchema,
  constraints: constraintsSchema,
  plannedAt: z.string().nullable().optional(),
  progress: progressSchema,
  createdAt: z.string().min(1),
  updatedAt: z.string().min(1),
});

export const deliveryPageSchema = z.object({
  items: z.array(deliverySchema),
  nextCursor: z.string().nullable(),
});

export type Delivery = z.infer<typeof deliverySchema>;
export type DeliveryPage = z.infer<typeof deliveryPageSchema>;

export interface DeliveryDraft {
  pickupAddress: string;
  pickupLatitude: string;
  pickupLongitude: string;
  pickupInstructions: string;
  destinationAddress: string;
  destinationLatitude: string;
  destinationLongitude: string;
  destinationInstructions: string;
  cargoDescription: string;
  weightKg: string;
  parcelCount: string;
  windowStart: string;
  windowEnd: string;
  vehicleKinds: VehicleKind[];
  fragile: boolean;
  tempMin: string;
  tempMax: string;
}

export function dublinDemoDraft(now = Date.now()): DeliveryDraft {
  const departure = now + 30 * 60_000;
  return {
    pickupAddress: "Dublin Port",
    pickupLatitude: "53.3478",
    pickupLongitude: "-6.2297",
    pickupInstructions: "",
    destinationAddress: "Trinity College Dublin",
    destinationLatitude: "53.3438",
    destinationLongitude: "-6.2546",
    destinationInstructions: "",
    cargoDescription: "Office supplies",
    weightKg: "4.5",
    parcelCount: "2",
    windowStart: new Date(departure).toISOString(),
    windowEnd: new Date(departure + 3 * 60 * 60_000).toISOString(),
    vehicleKinds: ["van", "cargo_bike"],
    fragile: false,
    tempMin: "",
    tempMax: "",
  };
}

export interface DeliveryPayloadFields {
  pickup: {
    address: string;
    latitude: number;
    longitude: number;
    instructions?: string;
  };
  destination: {
    address: string;
    latitude: number;
    longitude: number;
    instructions?: string;
  };
  cargo: {
    description: string;
    weightKg: number;
    parcelCount: number;
  };
  timeWindow: {
    start: string;
    end: string;
  };
  constraints: {
    vehicleKinds: VehicleKind[];
    fragile: boolean;
    temperatureRangeC?: { minimum: number; maximum: number };
  };
}

export class DeliveryAccessDenied extends Error {}

export class DeliveryRequestError extends Error {
  readonly code?: string;
  readonly fieldErrors: Record<string, string>;

  constructor(message: string, code?: string, fieldErrors: Record<string, string> = {}) {
    super(message);
    this.code = code;
    this.fieldErrors = fieldErrors;
  }
}

export interface SubmitDeliveryResult {
  delivery: Delivery;
  runId: string | null;
  replayed: boolean;
  correlationId: string | null;
  planId: string | null;
  planVersion: string | null;
  planStub: boolean;
}

export function validateDraft(
  draft: DeliveryDraft,
):
  | { payload: DeliveryPayloadFields; errors: null }
  | { payload: null; errors: Record<string, string> } {
  const errors: Record<string, string> = {};

  const pickupAddress = draft.pickupAddress.trim();
  const destAddress = draft.destinationAddress.trim();
  const cargoDescription = draft.cargoDescription.trim();

  if (!pickupAddress) errors.pickupAddress = "Pickup address is required.";
  if (!destAddress) errors.destinationAddress = "Destination address is required.";
  if (!cargoDescription) errors.cargoDescription = "Cargo description is required.";

  const pickupLat = Number(draft.pickupLatitude);
  const pickupLon = Number(draft.pickupLongitude);
  if (!Number.isFinite(pickupLat) || pickupLat < -90 || pickupLat > 90) {
    errors.pickupLatitude = "Latitude must be between -90 and 90.";
  }
  if (!Number.isFinite(pickupLon) || pickupLon < -180 || pickupLon > 180) {
    errors.pickupLongitude = "Longitude must be between -180 and 180.";
  }

  const destLat = Number(draft.destinationLatitude);
  const destLon = Number(draft.destinationLongitude);
  if (!Number.isFinite(destLat) || destLat < -90 || destLat > 90) {
    errors.destinationLatitude = "Latitude must be between -90 and 90.";
  }
  if (!Number.isFinite(destLon) || destLon < -180 || destLon > 180) {
    errors.destinationLongitude = "Longitude must be between -180 and 180.";
  }

  if (
    !errors.pickupLatitude &&
    !errors.pickupLongitude &&
    !errors.destinationLatitude &&
    !errors.destinationLongitude &&
    Math.abs(pickupLat - destLat) < 1e-6 &&
    Math.abs(pickupLon - destLon) < 1e-6
  ) {
    errors.destinationAddress = "Destination must differ from pickup location.";
  }

  const weightKg = Number(draft.weightKg);
  if (!Number.isFinite(weightKg) || weightKg <= 0) {
    errors.weightKg = "Weight must be greater than 0 kg.";
  }

  const parcelCount = Number(draft.parcelCount);
  if (!Number.isInteger(parcelCount) || parcelCount < 1) {
    errors.parcelCount = "Parcel count must be a whole number of at least 1.";
  }

  const startStr = draft.windowStart.trim();
  const endStr = draft.windowEnd.trim();
  const startDate = Date.parse(startStr);
  const endDate = Date.parse(endStr);
  if (!startStr || Number.isNaN(startDate) || !startStr.endsWith("Z")) {
    errors.windowStart = "Use a UTC RFC 3339 timestamp ending in Z.";
  }
  if (!endStr || Number.isNaN(endDate) || !endStr.endsWith("Z")) {
    errors.windowEnd = "Use a UTC RFC 3339 timestamp ending in Z.";
  }
  if (!errors.windowStart && !errors.windowEnd && endDate <= startDate) {
    errors.windowEnd = "Window end must be after start.";
  }

  if (draft.vehicleKinds.length === 0) {
    errors.vehicleKinds = "Select at least one permitted vehicle type.";
  }

  let temperatureRangeC: { minimum: number; maximum: number } | undefined;
  const hasMin = draft.tempMin.trim() !== "";
  const hasMax = draft.tempMax.trim() !== "";
  if (hasMin || hasMax) {
    const minVal = Number(draft.tempMin);
    const maxVal = Number(draft.tempMax);
    if (!hasMin || !Number.isFinite(minVal)) {
      errors.tempMin = "Enter a numeric minimum temperature.";
    }
    if (!hasMax || !Number.isFinite(maxVal)) {
      errors.tempMax = "Enter a numeric maximum temperature.";
    }
    if (!errors.tempMin && !errors.tempMax) {
      if (minVal > maxVal) {
        errors.tempMax = "Maximum temperature must be at least the minimum.";
      } else {
        temperatureRangeC = { minimum: minVal, maximum: maxVal };
      }
    }
  }

  if (Object.keys(errors).length > 0) {
    return { payload: null, errors };
  }

  const pickupInstructions = draft.pickupInstructions.trim();
  const destinationInstructions = draft.destinationInstructions.trim();

  return {
    errors: null,
    payload: {
      pickup: {
        address: pickupAddress,
        latitude: pickupLat,
        longitude: pickupLon,
        ...(pickupInstructions ? { instructions: pickupInstructions } : {}),
      },
      destination: {
        address: destAddress,
        latitude: destLat,
        longitude: destLon,
        ...(destinationInstructions ? { instructions: destinationInstructions } : {}),
      },
      cargo: {
        description: cargoDescription,
        weightKg,
        parcelCount,
      },
      timeWindow: {
        start: startStr,
        end: endStr,
      },
      constraints: {
        vehicleKinds: [...draft.vehicleKinds],
        fragile: draft.fragile,
        ...(temperatureRangeC ? { temperatureRangeC } : {}),
      },
    },
  };
}

export function draftFromDelivery(delivery: Delivery): DeliveryDraft {
  return {
    pickupAddress: delivery.pickup.address,
    pickupLatitude: String(delivery.pickup.latitude),
    pickupLongitude: String(delivery.pickup.longitude),
    pickupInstructions: delivery.pickup.instructions ?? "",
    destinationAddress: delivery.destination.address,
    destinationLatitude: String(delivery.destination.latitude),
    destinationLongitude: String(delivery.destination.longitude),
    destinationInstructions: delivery.destination.instructions ?? "",
    cargoDescription: delivery.cargo.description,
    weightKg: String(delivery.cargo.weightKg),
    parcelCount: String(delivery.cargo.parcelCount),
    windowStart: delivery.timeWindow.start,
    windowEnd: delivery.timeWindow.end,
    vehicleKinds: [...delivery.constraints.vehicleKinds],
    fragile: delivery.constraints.fragile,
    tempMin:
      delivery.constraints.temperatureRangeC != null
        ? String(delivery.constraints.temperatureRangeC.minimum)
        : "",
    tempMax:
      delivery.constraints.temperatureRangeC != null
        ? String(delivery.constraints.temperatureRangeC.maximum)
        : "",
  };
}

const SERVER_PATH_TO_FIELD: Record<string, string> = {
  "body.pickup.address": "pickupAddress",
  "body.pickup.latitude": "pickupLatitude",
  "body.pickup.longitude": "pickupLongitude",
  "body.destination.address": "destinationAddress",
  "body.destination.latitude": "destinationLatitude",
  "body.destination.longitude": "destinationLongitude",
  "body.cargo.description": "cargoDescription",
  "body.cargo.weightKg": "weightKg",
  "body.cargo.parcelCount": "parcelCount",
  "body.timeWindow.start": "windowStart",
  "body.timeWindow.end": "windowEnd",
  "body.timeWindow": "windowEnd",
  "body.constraints.vehicleKinds": "vehicleKinds",
  "body.constraints.temperatureRangeC": "tempMax",
};

function parseApiError(body: unknown): DeliveryRequestError {
  if (typeof body === "object" && body !== null && "error" in body) {
    const err = (body as { error?: Record<string, unknown> }).error;
    const message = typeof err?.message === "string" ? err.message : "Delivery request failed.";
    const code = typeof err?.code === "string" ? err.code : undefined;
    const fieldErrors: Record<string, string> = {};
    if (Array.isArray(err?.fields)) {
      for (const entry of err.fields) {
        if (
          typeof entry === "object" &&
          entry !== null &&
          typeof (entry as { path?: unknown }).path === "string" &&
          typeof (entry as { message?: unknown }).message === "string"
        ) {
          const path = (entry as { path: string }).path;
          const mapped = SERVER_PATH_TO_FIELD[path] ?? path;
          fieldErrors[mapped] = (entry as { message: string }).message;
        }
      }
    }
    return new DeliveryRequestError(message, code, fieldErrors);
  }
  return new DeliveryRequestError("Delivery request failed.");
}

function querySuffix(params: Record<string, string | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value) search.set(key, value);
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

export async function listDeliveries(
  authenticatedRequest: SessionRequest,
  providerId: string,
  options: { runId?: string; status?: DeliveryStatus | ""; signal?: AbortSignal },
): Promise<DeliveryPage> {
  const headers = new Headers({ "X-Acting-Provider": providerId });
  const suffix = querySuffix({
    runId: options.runId || undefined,
    status: options.status || undefined,
    limit: "100",
  });
  const response = await authenticatedRequest(`/deliveries${suffix}`, {
    method: "GET",
    headers,
    signal: options.signal,
  });
  options.signal?.throwIfAborted();
  if (response.status === 403) throw new DeliveryAccessDenied();
  const body = await response.json();
  if (!response.ok) throw parseApiError(body);
  return deliveryPageSchema.parse(body);
}

export async function submitDeliveryRequest(
  authenticatedRequest: SessionRequest,
  providerId: string,
  payload: DeliveryPayloadFields,
  options: {
    idempotencyKey: string;
    correlationId: string;
    runId?: string;
    signal?: AbortSignal;
  },
): Promise<SubmitDeliveryResult> {
  const headers = new Headers({
    "Content-Type": "application/json",
    "X-Acting-Provider": providerId,
    "X-Correlation-ID": options.correlationId,
  });
  const suffix = querySuffix({ runId: options.runId || undefined });
  const response = await authenticatedRequest(`/deliveries${suffix}`, {
    method: "POST",
    headers,
    signal: options.signal,
    body: JSON.stringify({ ...payload, idempotencyKey: options.idempotencyKey }),
  });
  options.signal?.throwIfAborted();
  if (response.status === 403) throw new DeliveryAccessDenied();
  const body = await response.json();
  if (!response.ok) throw parseApiError(body);
  return {
    delivery: deliverySchema.parse(body),
    runId: options.runId || null,
    replayed: response.headers.get("Idempotency-Replayed") === "true",
    correlationId: response.headers.get("X-Correlation-ID") ?? options.correlationId,
    planId: response.headers.get("X-Plan-Id"),
    planVersion: response.headers.get("X-Plan-Version"),
    planStub: response.headers.get("X-Plan-Stub") === "true",
  };
}

export async function updateDeliveryRequest(
  authenticatedRequest: SessionRequest,
  providerId: string,
  deliveryId: string,
  payload: DeliveryPayloadFields,
  options: {
    expectedVersion: number;
    idempotencyKey: string;
    correlationId: string;
    runId?: string;
    signal?: AbortSignal;
  },
): Promise<Delivery> {
  const headers = new Headers({
    "Content-Type": "application/json",
    "X-Acting-Provider": providerId,
    "X-Correlation-ID": options.correlationId,
  });
  const suffix = querySuffix({ runId: options.runId || undefined });
  const response = await authenticatedRequest(`/deliveries/${deliveryId}${suffix}`, {
    method: "POST",
    headers,
    signal: options.signal,
    body: JSON.stringify({
      ...payload,
      expectedVersion: options.expectedVersion,
      idempotencyKey: options.idempotencyKey,
    }),
  });
  options.signal?.throwIfAborted();
  if (response.status === 403) throw new DeliveryAccessDenied();
  const body = await response.json();
  if (!response.ok) throw parseApiError(body);
  return deliverySchema.parse(body);
}
