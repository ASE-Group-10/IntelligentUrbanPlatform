import { createFileRoute } from "@tanstack/react-router";

import { ScreenPlaceholder } from "@/components/layout/screen-placeholder";

export const Route = createFileRoute("/plans")({
  component: () => (
    <ScreenPlaceholder
      title="Plans"
      description="Proposed and confirmed delivery plans, their routes and explanations."
      component="C05 Planning · C08 Allocation · C10 Explanation"
      owners="Ye + Zecheng, Shixian + Oscar, Qingyuan + Bangchen"
      planned={[
        "Plan proposal with route on the map",
        "Operator confirmation",
        "Allocation and driver acceptance (UC04)",
        "Grounded explanation of each decision",
      ]}
    />
  ),
});
