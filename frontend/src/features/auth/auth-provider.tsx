import type { Session, SupabaseClient } from "@supabase/supabase-js";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { AuthContext, type Auth } from "./auth-context";
import { identitySchema, sessionRequest } from "./identity";
import { supabase } from "./supabase";

/**
 * One Supabase session for the whole app (#96): every screen and the top bar
 * read the same user, acting provider and token.
 */
export function AuthProvider({
  children,
  client = supabase,
}: {
  children: ReactNode;
  client?: SupabaseClient | null;
}) {
  const [session, setSession] = useState<Session | null | undefined>(client ? undefined : null);
  // The choice belongs to one user; another account starts without one.
  const [choice, setChoice] = useState({ principalId: "", providerId: "" });
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!client) return;
    const { data } = client.auth.onAuthStateChange((event, nextSession) => {
      setSession(nextSession);
      if (event === "SIGNED_OUT") queryClient.removeQueries({ queryKey: ["current-identity"] });
    });
    return () => data.subscription.unsubscribe();
  }, [client, queryClient]);

  const expire = useCallback(async () => {
    setSession(null);
    queryClient.removeQueries({ queryKey: ["current-identity"] });
    await client?.auth.signOut({ scope: "local" }).catch(() => undefined);
  }, [client, queryClient]);

  const principalId = session?.user.id;
  const request = useMemo(
    () => (client && principalId ? sessionRequest(client, principalId, expire) : undefined),
    [client, principalId, expire],
  );

  const identity = useQuery({
    queryKey: ["current-identity", principalId],
    enabled: Boolean(request),
    gcTime: 0,
    retry: false,
    queryFn: async ({ signal }) => {
      if (!request) throw new Error("Not signed in.");
      const response = await request("/identity/me", { signal });
      if (!response.ok) throw new Error("Couldn’t verify your access.");
      const profile = identitySchema.parse(await response.json());
      if (profile.principalId !== principalId) throw new Error("Account changed.");
      return profile;
    },
  });

  const signOut = useCallback(async () => {
    if (!client) return true;
    try {
      const { error } = await client.auth.signOut({ scope: "local" });
      return !error;
    } catch {
      return false;
    }
  }, [client]);

  const profile = request ? identity.data : undefined;
  const providers = useMemo(() => profile?.availableProviders ?? [], [profile]);
  const admin = profile?.platformRoles.includes("admin") ?? false;
  const needsSelection = admin || providers.length > 1;
  const chosen = choice.principalId === principalId ? choice.providerId : "";
  const actingProvider = needsSelection
    ? providers.find((entry) => entry.providerId === chosen)
    : providers[0];
  const canRegister =
    admin ||
    Boolean(
      profile?.memberships.some(
        (entry) =>
          entry.providerId === actingProvider?.providerId && entry.roles.includes("manager"),
      ),
    );

  const value: Auth = {
    client,
    session,
    accessToken: session?.access_token,
    request,
    identity: profile,
    identityStatus: !request ? "idle" : identity.status,
    retryIdentity: () => void identity.refetch(),
    providers,
    needsSelection,
    actingProvider,
    selectProvider: (providerId) => setChoice({ principalId: principalId ?? "", providerId }),
    canRegister,
    signOut,
  };

  return <AuthContext value={value}>{children}</AuthContext>;
}
