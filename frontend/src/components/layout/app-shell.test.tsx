import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { queryClient } from "@/lib/query-client";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

import { DESKTOP_QUERY } from "./breakpoints";
import { NAV_ITEMS } from "./nav-items";

/** matchMedia stand-in whose answers the test can change, firing listeners. */
function fakeMediaQueries() {
  const listeners = new Map<string, Set<(event: MediaQueryListEvent) => void>>();
  const matches = new Map<string, boolean>();
  window.matchMedia = (query: string) =>
    ({
      media: query,
      get matches() {
        return matches.get(query) ?? false;
      },
      addEventListener: (_: string, fn: (event: MediaQueryListEvent) => void) => {
        if (!listeners.has(query)) listeners.set(query, new Set());
        listeners.get(query)?.add(fn);
      },
      removeEventListener: (_: string, fn: (event: MediaQueryListEvent) => void) => {
        listeners.get(query)?.delete(fn);
      },
    }) as unknown as MediaQueryList;
  return (query: string, value: boolean) => {
    matches.set(query, value);
    for (const fn of listeners.get(query) ?? []) {
      fn({ matches: value, media: query } as MediaQueryListEvent);
    }
  };
}

async function renderAt(path: string) {
  const router = createRouter({
    routeTree,
    context: { queryClient },
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  render(
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ThemeProvider>,
  );
  await router.load();
  return router;
}

describe("AppShell", () => {
  it("links to every thin-slice screen", async () => {
    await renderAt("/resources");

    const nav = await screen.findByRole("navigation", { name: "Main" });
    for (const item of NAV_ITEMS) {
      expect(nav).toHaveTextContent(item.label);
    }
  });

  it("marks the current screen as active", async () => {
    await renderAt("/plans");

    const nav = await screen.findByRole("navigation", { name: "Main" });
    const active = nav.querySelector('[data-status="active"]');
    expect(active).toHaveTextContent("Plans");
  });

  it("labels the data as simulated", async () => {
    await renderAt("/kpis");

    expect(await screen.findByText("Simulation")).toBeInTheDocument();
  });

  it("shows a helpful page for unknown addresses", async () => {
    await renderAt("/nowhere");

    expect(await screen.findByText("This page does not exist")).toBeInTheDocument();
  });
});

describe("Phone menu", () => {
  it("closes after choosing a screen", async () => {
    fakeMediaQueries();
    const user = userEvent.setup();
    const router = await renderAt("/resources");

    await user.click(screen.getByRole("button", { name: "Open menu" }));
    const menu = await screen.findByRole("dialog");
    await user.click(within(menu).getByRole("link", { name: /Plans/ }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(router.state.location.pathname).toBe("/plans");
  });

  it("closes when the window grows to desktop width", async () => {
    const setMedia = fakeMediaQueries();
    const user = userEvent.setup();
    await renderAt("/resources");

    await user.click(screen.getByRole("button", { name: "Open menu" }));
    await screen.findByRole("dialog");
    act(() => setMedia(DESKTOP_QUERY, true));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});
