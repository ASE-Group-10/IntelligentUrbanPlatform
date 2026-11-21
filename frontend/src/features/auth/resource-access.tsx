import type { Session, SupabaseClient } from "@supabase/supabase-js";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";

import { Button } from "@/components/ui/button";

import { identitySchema, sessionRequest, type SessionRequest } from "./identity";
import { supabase } from "./supabase";

export interface ResourceScope {
  providerId: string;
  canRegister: boolean;
  request: SessionRequest;
}

type Workspace = (scope: ResourceScope) => ReactNode;

export function ResourceAccess({
  children,
  area = "Resources",
}: {
  children: Workspace;
  area?: string;
}) {
  if (!supabase) {
    return <AccessNotice>{area} sign-in is unavailable in this environment.</AccessNotice>;
  }

  return (
    <ResourceSession client={supabase} area={area}>
      {children}
    </ResourceSession>
  );
}

function ResourceSession({
  client,
  children,
  area,
}: {
  client: SupabaseClient;
  children: Workspace;
  area: string;
}) {
  const [session, setSession] = useState<Session | null | undefined>();
  const queryClient = useQueryClient();

  useEffect(() => {
    const { data } = client.auth.onAuthStateChange((event, nextSession) => {
      setSession(nextSession);
      if (event === "SIGNED_OUT") {
        queryClient.removeQueries({ queryKey: ["current-identity"] });
      }
    });
    return () => data.subscription.unsubscribe();
  }, [client, queryClient]);

  const expire = useCallback(async () => {
    setSession(null);
    queryClient.removeQueries({ queryKey: ["current-identity"] });
    await client.auth.signOut({ scope: "local" }).catch(() => undefined);
  }, [client, queryClient]);

  if (session === undefined) return <AccessNotice>Checking your session…</AccessNotice>;
  if (!session) return <SignIn client={client} area={area} />;

  return (
    <SignedInResources
      key={session.user.id}
      client={client}
      principalId={session.user.id}
      expire={expire}
    >
      {children}
    </SignedInResources>
  );
}

function SignIn({ client, area }: { client: SupabaseClient; area: string }) {
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState(false);

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setPending(true);
    setFailed(false);
    try {
      const { error } = await client.auth.signInWithPassword({
        email: String(form.get("email")),
        password: String(form.get("password")),
      });
      setFailed(Boolean(error));
    } catch {
      setFailed(true);
    } finally {
      setPending(false);
    }
  }

  const inputClass = "w-full rounded-lg border border-input bg-background px-3 py-2 text-sm";

  return (
    <form
      onSubmit={signIn}
      className="mx-auto my-8 grid w-full max-w-sm gap-4 rounded-xl border bg-card p-6 text-card-foreground"
    >
      <h1 className="font-heading text-xl font-semibold">Sign in to {area}</h1>
      <label className="grid gap-1 text-sm">
        Email
        <input className={inputClass} name="email" type="email" autoComplete="username" required />
      </label>
      <label className="grid gap-1 text-sm">
        Password
        <input
          className={inputClass}
          name="password"
          type="password"
          autoComplete="current-password"
          required
        />
      </label>
      {failed && (
        <p role="alert" className="text-sm text-destructive">
          Sign-in failed. Check your details and try again.
        </p>
      )}
      <Button type="submit" disabled={pending}>
        {pending ? "Signing in…" : "Sign in"}
      </Button>
    </form>
  );
}

function SignedInResources({
  client,
  principalId,
  expire,
  children,
}: {
  client: SupabaseClient;
  principalId: string;
  expire: () => Promise<void>;
  children: Workspace;
}) {
  const [providerId, setProviderId] = useState("");
  const [signOutError, setSignOutError] = useState(false);
  const request = useMemo(
    () => sessionRequest(client, principalId, expire),
    [client, principalId, expire],
  );
  const identity = useQuery({
    queryKey: ["current-identity", principalId],
    gcTime: 0,
    queryFn: async ({ signal }) => {
      const response = await request("/identity/me", { signal });
      if (!response.ok) throw new Error("Couldn’t verify your access.");
      const profile = identitySchema.parse(await response.json());
      if (profile.principalId !== principalId) throw new Error("Account changed.");
      return profile;
    },
    retry: false,
  });

  async function signOut() {
    setSignOutError(false);
    try {
      const { error } = await client.auth.signOut({ scope: "local" });
      setSignOutError(Boolean(error));
    } catch {
      setSignOutError(true);
    }
  }

  const profile = identity.data;
  const providers = profile?.availableProviders ?? [];
  const admin = profile?.platformRoles.includes("admin") ?? false;
  const needsSelection = admin || providers.length > 1;
  const actingProvider = needsSelection
    ? providers.find((entry) => entry.providerId === providerId)
    : providers[0];
  const canRegister =
    admin ||
    Boolean(
      profile?.memberships.some(
        (entry) =>
          entry.providerId === actingProvider?.providerId && entry.roles.includes("manager"),
      ),
    );
  return (
    <>
      <div className="flex flex-wrap items-center justify-end gap-3 border-b px-4 py-2 text-sm">
        {identity.data && <span>{identity.data.displayName}</span>}
        <Button variant="outline" onClick={signOut}>
          Sign out
        </Button>
        {signOutError && <p role="alert">Sign-out failed. Try again.</p>}
      </div>
      {identity.isPending && <AccessNotice>Checking provider access…</AccessNotice>}
      {identity.isError && (
        <AccessNotice>
          <span role="alert">Couldn’t verify your access.</span>{" "}
          <Button variant="outline" onClick={() => void identity.refetch()}>
            Retry
          </Button>
        </AccessNotice>
      )}
      {identity.isSuccess && identity.data && (
        <>
          {needsSelection && providers.length > 0 && (
            <label className="flex items-center gap-2 px-4 py-3 text-sm">
              Acting provider
              <select
                className="rounded-lg border border-input bg-background px-2 py-1"
                value={actingProvider?.providerId ?? ""}
                onChange={(event) => setProviderId(event.target.value)}
              >
                <option value="" disabled>
                  Select a provider
                </option>
                {providers.map((membership) => (
                  <option key={membership.providerId} value={membership.providerId}>
                    {membership.providerName}
                  </option>
                ))}
              </select>
            </label>
          )}
          {providers.length === 0 && (
            <AccessNotice>You don’t have access to a provider yet.</AccessNotice>
          )}
          {actingProvider && (
            <div key={actingProvider.providerId}>
              {!needsSelection && (
                <p className="px-4 py-2 text-sm">Acting provider: {actingProvider.providerName}</p>
              )}
              {children({ providerId: actingProvider.providerId, canRegister, request })}
            </div>
          )}
        </>
      )}
    </>
  );
}

function AccessNotice({ children }: { children: ReactNode }) {
  return <div className="p-6 text-sm text-muted-foreground">{children}</div>;
}
