import type { LucideIcon } from "lucide-react";
import { BarChart3, Boxes, Map, PackageCheck, Route, TriangleAlert } from "lucide-react";

export interface NavItem {
  to: "/" | "/resources" | "/deliveries" | "/plans" | "/disruptions" | "/kpis";
  label: string;
  icon: LucideIcon;
  /** Architecture component that owns the screen, shown to help pairs find their area. */
  component: string;
}

export const NAV_ITEMS: readonly NavItem[] = [
  { to: "/", label: "City map", icon: Map, component: "C06" },
  { to: "/resources", label: "Resources", icon: Boxes, component: "C03" },
  { to: "/deliveries", label: "Deliveries", icon: PackageCheck, component: "C04" },
  { to: "/plans", label: "Plans", icon: Route, component: "C05 · C08" },
  { to: "/disruptions", label: "Disruptions", icon: TriangleAlert, component: "C07" },
  { to: "/kpis", label: "KPIs", icon: BarChart3, component: "C11" },
];
