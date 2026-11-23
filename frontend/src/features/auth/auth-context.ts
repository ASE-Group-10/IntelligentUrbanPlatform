import type { Session, SupabaseClient } from "@supabase/supabase-js";
import { createContext, useContext } from "react";
import type { z } from "zod";

import type { identitySchema, SessionRequest } from "./identity";

export type Identity = z.infer<typeof identitySchema>;
export type Provider = Identity["availableProviders"][number];

export interface Auth {
  /** `null` when this build has no Supabase keys, so nobody can sign in. */
  client: SupabaseClient | null;
  /** `undefined` while Supabase restores the stored session. */
  session: Session | null | undefined;
  /** Bearer token for API calls; Supabase refreshes it and the context follows. */
  accessToken?: string;
  /** Authenticated request helper for the signed-in user (see `sessionRequest`). */
  request?: SessionRequest;
  identity?: Identity;
  identityStatus: "idle" | "pending" | "error" | "success";
  retryIdentity: () => void;
  providers: Provider[];
  /** Platform admins and multi-provider users have to pick who they act for. */
  needsSelection: boolean;
  actingProvider?: Provider;
  selectProvider: (providerId: string) => void;
  canRegister: boolean;
  signOut: () => Promise<boolean>;
}

export const AuthContext = createContext<Auth | null>(null);

export function useAuth(): Auth {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("useAuth must be used inside AuthProvider");
  return auth;
}
