import { useState, type FormEvent, type ReactNode } from "react";

import { Button } from "@/components/ui/button";

import { useAuth } from "./auth-context";
import type { SessionRequest } from "./identity";

export interface ResourceScope {
  providerId: string;
  canRegister: boolean;
  request: SessionRequest;
}

type Workspace = (scope: ResourceScope) => ReactNode;

/**
 * Guard for protected screens. The session lives in `AuthProvider`; this shows
 * the sign-in form, access checks and provider choice until there is an acting
 * provider, then renders the screen for it.
 */
export function ResourceAccess({
  children,
  area = "Resources",
}: {
  children: Workspace;
  area?: string;
}) {
  const auth = useAuth();

  if (!auth.client) {
    return <AccessNotice>{area} sign-in is unavailable in this environment.</AccessNotice>;
  }
  if (auth.session === undefined) return <AccessNotice>Checking your session…</AccessNotice>;
  if (!auth.session || !auth.request) return <SignInForm area={area} />;
  if (auth.identityStatus === "pending") {
    return <AccessNotice>Checking provider access…</AccessNotice>;
  }
  if (auth.identityStatus === "error") {
    return (
      <AccessNotice>
        <span role="alert">Couldn’t verify your access.</span>{" "}
        <Button variant="outline" onClick={auth.retryIdentity}>
          Retry
        </Button>
      </AccessNotice>
    );
  }
  if (auth.providers.length === 0) {
    return <AccessNotice>You don’t have access to a provider yet.</AccessNotice>;
  }
  if (!auth.actingProvider) {
    return (
      <AccessNotice>
        <span className="mb-3 block">Choose the provider you’re acting for.</span>
        <ProviderPicker />
      </AccessNotice>
    );
  }

  return (
    <div key={`${auth.session.user.id}:${auth.actingProvider.providerId}`}>
      {children({
        providerId: auth.actingProvider.providerId,
        canRegister: auth.canRegister,
        request: auth.request,
      })}
    </div>
  );
}

/** Acting-provider choice for a page header; a plain label when there is no choice. */
export function ProviderPicker() {
  const { providers, needsSelection, actingProvider, selectProvider } = useAuth();
  if (providers.length === 0) return null;
  if (!needsSelection) {
    return (
      <p className="text-sm text-muted-foreground">Acting for {actingProvider?.providerName}</p>
    );
  }

  return (
    <label className="flex items-center gap-2 text-sm">
      Acting provider
      <select
        className="rounded-lg border border-input bg-background px-2 py-1"
        value={actingProvider?.providerId ?? ""}
        onChange={(event) => selectProvider(event.target.value)}
      >
        <option value="" disabled>
          Select a provider
        </option>
        {providers.map((provider) => (
          <option key={provider.providerId} value={provider.providerId}>
            {provider.providerName}
          </option>
        ))}
      </select>
    </label>
  );
}

export function SignInForm({ area, className }: { area?: string; className?: string }) {
  const { client } = useAuth();
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState(false);

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!client) return;
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
      aria-label="Sign in"
      className={
        className ??
        "mx-auto my-8 grid w-full max-w-sm gap-4 rounded-xl border bg-card p-6 text-card-foreground"
      }
    >
      <h1 className="font-heading text-xl font-semibold">
        {area ? `Sign in to ${area}` : "Sign in"}
      </h1>
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

function AccessNotice({ children }: { children: ReactNode }) {
  return <div className="p-6 text-sm text-muted-foreground">{children}</div>;
}
