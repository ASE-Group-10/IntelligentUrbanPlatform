import { LogIn } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { useAuth } from "@/features/auth/auth-context";
import { SignInForm } from "@/features/auth/resource-access";

/** Signed-in user and sign-out, or a sign-in panel, in the top bar. */
export function UserMenu() {
  const { client, session, identity, signOut } = useAuth();
  const [signInOpen, setSignInOpen] = useState(false);
  const [signOutFailed, setSignOutFailed] = useState(false);

  if (!client || session === undefined) return null;

  if (!session) {
    return (
      <Sheet open={signInOpen} onOpenChange={setSignInOpen}>
        <SheetTrigger render={<Button variant="outline" size="sm" />}>
          <LogIn />
          Sign in
        </SheetTrigger>
        <SheetContent side="right" className="w-80 p-4">
          <SheetTitle className="sr-only">Sign in</SheetTitle>
          <SignInForm className="grid gap-4 pt-6" />
        </SheetContent>
      </Sheet>
    );
  }

  return (
    <div className="flex items-center gap-2 text-sm">
      {identity && (
        <span className="hidden max-w-40 truncate sm:inline">{identity.displayName}</span>
      )}
      <Button
        variant="outline"
        size="sm"
        onClick={async () => {
          setSignOutFailed(false);
          setSignOutFailed(!(await signOut()));
        }}
      >
        Sign out
      </Button>
      {signOutFailed && (
        <span role="alert" className="text-destructive">
          Sign-out failed. Try again.
        </span>
      )}
    </div>
  );
}
