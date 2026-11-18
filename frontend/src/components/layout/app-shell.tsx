import { Link } from "@tanstack/react-router";
import { Menu } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

import { DESKTOP_QUERY } from "./breakpoints";
import { NAV_ITEMS } from "./nav-items";
import { ThemeToggle } from "./theme-toggle";

/** Open state for the phone menu; it closes itself once the sidebar takes over. */
function usePhoneMenu() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const desktop = window.matchMedia(DESKTOP_QUERY);
    const onChange = (event: MediaQueryListEvent) => {
      if (event.matches) setOpen(false);
    };
    desktop.addEventListener("change", onChange);
    return () => desktop.removeEventListener("change", onChange);
  }, []);

  return [open, setOpen] as const;
}

function Brand({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <Link to="/" onClick={onNavigate} className="flex items-center gap-2.5 px-2">
      <span className="grid size-8 place-items-center rounded-lg bg-primary text-primary-foreground">
        <svg viewBox="0 0 32 32" className="size-5" aria-hidden="true">
          <path
            d="M7 22c4-8 12-4 18-12"
            stroke="currentColor"
            strokeWidth="3"
            fill="none"
            strokeLinecap="round"
          />
          <circle cx="7" cy="22" r="2.5" fill="currentColor" />
          <circle cx="25" cy="10" r="2.5" fill="currentColor" />
        </svg>
      </span>
      <span className="flex flex-col leading-tight">
        <span className="font-heading text-sm font-semibold tracking-tight">ULOP</span>
        <span className="text-[11px] text-muted-foreground">Urban logistics · Dublin</span>
      </span>
    </Link>
  );
}

function NavLinks({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav aria-label="Main" className="flex flex-col gap-1">
      {NAV_ITEMS.map(({ to, label, icon: Icon, component }) => (
        <Link
          key={to}
          to={to}
          onClick={onNavigate}
          activeOptions={{ exact: to === "/" }}
          className="group flex items-center gap-3 rounded-md px-2.5 py-2 text-sm text-sidebar-foreground/80 transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground data-[status=active]:bg-sidebar-accent data-[status=active]:font-medium data-[status=active]:text-sidebar-accent-foreground"
        >
          <Icon className="size-4 shrink-0 text-muted-foreground group-data-[status=active]:text-primary" />
          <span className="flex-1">{label}</span>
          <span className="font-mono text-[10px] text-muted-foreground/70">{component}</span>
        </Link>
      ))}
    </nav>
  );
}

/** Layout shared by every screen: sidebar on desktop, slide-out menu on phones. */
export function AppShell({ children }: { children: ReactNode }) {
  const [menuOpen, setMenuOpen] = usePhoneMenu();
  const closeMenu = () => setMenuOpen(false);

  return (
    <TooltipProvider>
      <div className="flex min-h-dvh">
        <aside className="hidden w-64 shrink-0 flex-col gap-6 border-r bg-sidebar p-3 lg:flex">
          <div className="pt-1">
            <Brand />
          </div>
          <NavLinks />
          <p className="mt-auto px-2 text-[11px] leading-relaxed text-muted-foreground">
            Map data © OpenStreetMap contributors · tiles by OpenFreeMap
          </p>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-20 flex h-14 items-center gap-2 border-b bg-background/85 px-3 backdrop-blur supports-[backdrop-filter]:bg-background/70 sm:px-4">
            <Sheet open={menuOpen} onOpenChange={setMenuOpen}>
              <SheetTrigger
                render={
                  <Button
                    variant="ghost"
                    size="icon"
                    className="lg:hidden"
                    aria-label="Open menu"
                  />
                }
              >
                <Menu className="size-5" />
              </SheetTrigger>
              <SheetContent side="left" className="w-72 bg-sidebar p-3">
                <SheetTitle className="sr-only">Navigation</SheetTitle>
                <div className="flex flex-col gap-6 pt-2">
                  <Brand onNavigate={closeMenu} />
                  <NavLinks onNavigate={closeMenu} />
                </div>
              </SheetContent>
            </Sheet>
            <div className="lg:hidden">
              <Brand />
            </div>

            <div className="ml-auto flex items-center gap-2">
              <Tooltip>
                <TooltipTrigger render={<span />}>
                  <Badge variant="outline" className="gap-1.5 font-normal">
                    <span className="size-1.5 rounded-full bg-status-slow" aria-hidden="true" />
                    Simulation
                  </Badge>
                </TooltipTrigger>
                <TooltipContent>Showing simulated data. No live commands are sent.</TooltipContent>
              </Tooltip>
              <ThemeToggle />
            </div>
          </header>

          <main className="flex min-h-0 flex-1 flex-col">{children}</main>
        </div>
      </div>
    </TooltipProvider>
  );
}
