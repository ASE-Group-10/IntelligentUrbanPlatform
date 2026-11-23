import { z } from "zod";

import type { SupabaseClient } from "@supabase/supabase-js";
import { config } from "@/lib/config";

export const identitySchema = z.object({
  principalId: z.uuid(),
  principalType: z.literal("user"),
  displayName: z.string(),
  memberships: z.array(
    z.object({
      providerId: z.uuid(),
      providerName: z.string(),
      roles: z.array(z.enum(["manager", "operator", "driver", "analyst", "curator"])).min(1),
    }),
  ),
  availableProviders: z.array(z.object({ providerId: z.uuid(), providerName: z.string() })),
  platformRoles: z.array(z.literal("admin")),
});

export type SessionRequest = (path: string, init: RequestInit) => Promise<Response>;

// Retry the same request once, retaining the acting provider and idempotency key.
export function sessionRequest(
  client: SupabaseClient,
  principalId: string,
  expire: () => Promise<void>,
): SessionRequest {
  return async (path, init) => {
    const { signal } = init;
    const send = (token: string) => {
      signal?.throwIfAborted();
      const headers = new Headers(init.headers);
      headers.set("Authorization", `Bearer ${token}`);
      return fetch(`${config.apiBaseUrl.replace(/\/$/, "")}/v1${path}`, { ...init, headers });
    };
    const { data } = await client.auth.getSession();
    signal?.throwIfAborted();
    if (data.session?.user.id === principalId) {
      const response = await send(data.session.access_token);
      if (response.status !== 401) return response;
      signal?.throwIfAborted();
      const refreshed = await client.auth.refreshSession().catch(() => null);
      signal?.throwIfAborted();
      const next = refreshed?.data.session;
      if (next && next.user.id !== principalId) throw new Error("Account changed.");
      if (next) {
        const retry = await send(next.access_token);
        if (retry.status !== 401) return retry;
      }
    } else if (data.session) {
      throw new Error("Account changed.");
    }
    signal?.throwIfAborted();
    await expire();
    throw new Error("Session expired. Sign in again.");
  };
}
