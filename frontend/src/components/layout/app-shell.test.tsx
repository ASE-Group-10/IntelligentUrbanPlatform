import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import { ThemeProvider } from "@/lib/theme";

import { queryClient } from "@/lib/query-client";
import { routeTree } from "@/routeTree.gen";

import { NAV_ITEMS } from "./nav-items";

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
