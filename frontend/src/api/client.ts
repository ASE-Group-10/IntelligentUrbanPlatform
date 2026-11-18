import createClient from "openapi-fetch";

import { config } from "@/lib/config";

import type { paths } from "./schema";

/**
 * Typed client for the backend. Request and response types come from the
 * FastAPI OpenAPI schema, so a backend change that breaks a screen fails the
 * frontend type check instead of failing at runtime.
 */
export const api = createClient<paths>({ baseUrl: config.apiBaseUrl });
